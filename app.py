import json
import re
import time
from io import BytesIO
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

APP_TITLE = "Faspex 5 Endpoint & OAuth Validator"
DEFAULT_BASE_URL = "https://faspex-5018-ariba.dev.fyre.ibm.com"
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
EXCEL_SHEET_CANDIDATES = ["All Endpoints", "All endpoints", "Endpoints"]

st.set_page_config(page_title=APP_TITLE, page_icon="🔐", layout="wide")
st.title(APP_TITLE)
st.caption("Compare an endpoint against your OpenAPI JSON and Excel matrix, then test it with User, Manager, and Admin Bearer tokens.")

with st.sidebar:
    st.header("1. Documentation files")
    st.markdown("Upload the OpenAPI JSON and the Excel scope matrix. The bundled project includes the versions you supplied.")
    openapi_file = st.file_uploader("OpenAPI JSON", type=["json"], key="openapi")
    matrix_file = st.file_uploader("Excel OAuth matrix", type=["xlsx"], key="matrix")
    st.divider()
    st.header("2. Faspex target")
    base_url = st.text_input("Base host", value=DEFAULT_BASE_URL).strip().rstrip("/")
    timeout_s = st.number_input("Request timeout (seconds)", min_value=3, max_value=120, value=20)
    verify_tls = st.checkbox("Verify TLS certificate", value=True, help="Keep enabled unless your test environment uses a trusted internal certificate chain.")
    follow_redirects = st.checkbox("Follow redirects", value=False, help="Off by default so 302 redirects are visible as their own result.")
    st.caption("Tokens stay in this browser session and are sent only to the configured Faspex host. Never paste tokens into tickets, chat, or source control.")

# Local bundled source files are used if the user doesn't upload files.
project_dir = __import__("pathlib").Path(__file__).resolve().parent
def get_bytes(upload, local_names):
    if upload is not None:
        return upload.getvalue(), upload.name
    for name in local_names:
        path = project_dir / name
        if path.exists():
            return path.read_bytes(), name
    return None, None

oas_bytes, oas_name = get_bytes(openapi_file, ["openapi (8).json", "openapi.json"])
xlsx_bytes, xlsx_name = get_bytes(matrix_file, ["Faspex_5_API_Endpoint_OAuth_Matrix(1).xlsx", "Faspex_5_API_Endpoint_OAuth_Matrix.xlsx"])

def normalize_path(path):
    path = (path or "").strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return re.sub(r"/+", "/", path)

def load_oas(raw):
    return json.loads(raw.decode("utf-8-sig"))

def get_operation(oas, method, path):
    paths = oas.get("paths", {})
    p = normalize_path(path)
    op = paths.get(p, {}).get(method.lower())
    if op:
        return p, op
    # Offer a relaxed matching fallback for accidental trailing slash only.
    p2 = p.rstrip("/") or "/"
    for candidate, item in paths.items():
        if candidate.rstrip("/") == p2 and method.lower() in item:
            return candidate, item[method.lower()]
    return p, None

def security_for_operation(op):
    security = op.get("security", None)
    if security is None:
        return "Not declared", [], []
    if security == []:
        return "No security required", [], []
    schemes = []
    scopes = []
    for req in security:
        for scheme, scope_list in req.items():
            schemes.append(scheme)
            if isinstance(scope_list, list):
                scopes.extend(str(s) for s in scope_list)
    return ", ".join(dict.fromkeys(schemes)) or "Not declared", list(dict.fromkeys(scopes)), security



def resolve_ref(oas, obj):
    """Resolve local OpenAPI $ref values, including chained schema references."""
    seen = set()
    while isinstance(obj, dict) and "$ref" in obj:
        ref = obj["$ref"]
        if ref in seen or not ref.startswith("#/"):
            return obj
        seen.add(ref)
        target = oas
        try:
            for part in ref[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            obj = target
        except (KeyError, TypeError):
            return obj
    return obj

def schema_example(oas, schema, name="value", depth=0):
    """Build a conservative editable example from OpenAPI schema metadata."""
    schema = resolve_ref(oas, schema or {})
    if not isinstance(schema, dict) or depth > 5:
        return f"REPLACE_WITH_{name}"
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("allOf"):
        merged = {}
        for part in schema["allOf"]:
            merged.update(schema_example(oas, part, name, depth + 1) if isinstance(schema_example(oas, part, name, depth + 1), dict) else {})
        return merged or f"REPLACE_WITH_{name}"
    typ = schema.get("type")
    if typ == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = schema.get("required", [])
        # Include required properties first, then documented optional properties with examples/defaults.
        names = list(dict.fromkeys(list(required) + [k for k, v in props.items() if isinstance(v, dict) and ("example" in v or "default" in v or "enum" in v)]))
        return {k: schema_example(oas, props.get(k, {}), k, depth + 1) for k in names}
    if typ == "array":
        item = schema_example(oas, schema.get("items", {}), name + "_item", depth + 1)
        return [item]
    if typ == "integer":
        return schema.get("minimum", 1)
    if typ == "number":
        return schema.get("minimum", 1)
    if typ == "boolean":
        return False
    if typ == "string":
        fmt = schema.get("format")
        if fmt == "email": return "user@example.com"
        if fmt == "date-time": return "2026-01-01T00:00:00Z"
        if fmt == "date": return "2026-01-01"
        if fmt == "uuid": return "00000000-0000-4000-8000-000000000000"
        return f"REPLACE_WITH_{name}"
    return f"REPLACE_WITH_{name}"

def operation_parameters(oas, path_item, operation):
    """Combine path-level and operation-level parameters and resolve parameter refs."""
    combined = []
    for raw in list(path_item.get("parameters", []) or []) + list(operation.get("parameters", []) or []):
        param = resolve_ref(oas, raw)
        if isinstance(param, dict):
            # Operation-level parameter with same name/location overrides path-level declaration.
            combined = [p for p in combined if not (p.get("name") == param.get("name") and p.get("in") == param.get("in"))]
            combined.append(param)
    return combined

def request_body_schema(oas, operation):
    body = resolve_ref(oas, operation.get("requestBody", {}))
    if not isinstance(body, dict):
        return {}, False, []
    content = body.get("content", {})
    media = next((v for k, v in content.items() if k.lower() == "application/json"), None)
    if media is None and content:
        media = next(iter(content.values()))
    media = media or {}
    schema = resolve_ref(oas, media.get("schema", {}))
    required = bool(body.get("required", False))
    examples = []
    if "example" in media:
        examples.append(media["example"])
    for example in (media.get("examples") or {}).values():
        ex = resolve_ref(oas, example)
        if isinstance(ex, dict) and "value" in ex:
            examples.append(ex["value"])
    return schema, required, examples

def pick_sheet(xl):
    preferred = next((s for s in EXCEL_SHEET_CANDIDATES if s in xl.sheet_names), None)
    if preferred:
        return preferred
    for sheet in xl.sheet_names:
        df = pd.read_excel(xl, sheet_name=sheet, nrows=2)
        cols = {str(c).strip().lower() for c in df.columns}
        if "method" in cols and "endpoint" in cols:
            return sheet
    return None

def matrix_row(xlsx_raw, method, path):
    try:
        xl = pd.ExcelFile(BytesIO(xlsx_raw))
        sheet = pick_sheet(xl)
        if not sheet:
            return None, "No worksheet with Method and Endpoint columns was found."
        df = pd.read_excel(xl, sheet_name=sheet, dtype=str).fillna("")
        df.columns = [str(c).strip() for c in df.columns]
        method_col = next((c for c in df.columns if c.lower() == "method"), None)
        endpoint_col = next((c for c in df.columns if c.lower() == "endpoint"), None)
        if not method_col or not endpoint_col:
            return None, f"Worksheet '{sheet}' does not contain Method and Endpoint columns."
        df["_method_norm"] = df[method_col].str.upper().str.strip()
        df["_endpoint_norm"] = df[endpoint_col].map(normalize_path)
        match = df[(df["_method_norm"] == method.upper()) & (df["_endpoint_norm"] == normalize_path(path))]
        if match.empty:
            return None, f"Endpoint not found in Excel worksheet '{sheet}'."
        return match.iloc[0].to_dict(), sheet
    except Exception as e:
        return None, f"Could not read Excel matrix: {e}"

def val(row, candidates):
    if not row:
        return ""
    for key in candidates:
        if key in row and str(row[key]).strip():
            return str(row[key]).strip()
    return ""

def status_class(code):
    if code is None:
        return "NOT RUN"
    if 200 <= code < 300:
        return "2xx — success"
    if code in (401, 403):
        return "401/403 — authentication or authorization denied"
    if 300 <= code < 400:
        return "3xx — redirect"
    if code in (400, 404, 405, 409, 422):
        return f"{code} — request/fixture/route needs review"
    return f"{code} — investigate"

if not oas_bytes or not xlsx_bytes:
    st.warning("Upload both files above, or run this app from the bundled project folder so it can use the included files.")
    st.stop()

try:
    oas = load_oas(oas_bytes)
except Exception as e:
    st.error(f"OpenAPI JSON could not be parsed: {e}")
    st.stop()

with st.expander("Loaded documentation", expanded=False):
    st.write(f"OpenAPI file: **{oas_name}**")
    st.write(f"Excel matrix: **{xlsx_name}**")
    st.write(f"OpenAPI paths: **{len(oas.get('paths', {}))}**")

st.header("3. Choose an endpoint")
paths = oas.get("paths", {})
available = []
for p, methods in paths.items():
    for m in methods:
        if m.lower() in [x.lower() for x in METHODS]:
            available.append((m.upper(), p))
available.sort(key=lambda x: (x[1], x[0]))

c1, c2 = st.columns([1, 4])
with c1:
    method = st.selectbox("HTTP method", METHODS, index=0)
with c2:
    discovered = [p for m, p in available if m == method]
    default_path = "/aspera/faspex/api/v5/advanced_configuration"
    if default_path not in discovered and discovered:
        default_path = discovered[0]
    selected_path = st.selectbox("Pick a documented path (or type below)", options=discovered or [default_path], index=(discovered.index(default_path) if default_path in discovered else 0))
path = st.text_input("Endpoint path", value=selected_path, help="Use the path exactly as documented, e.g. /aspera/faspex/api/v5/advanced_configuration")
path = normalize_path(path)

canonical_path, operation = get_operation(oas, method, path)
excel_row, excel_sheet = matrix_row(xlsx_bytes, method, canonical_path)

if not operation:
    st.error(f"No exact OpenAPI operation found for **{method} {path}**.")
    st.info("Choose a path from the dropdown or check the HTTP method and path spelling.")
    st.stop()

oas_auth, oas_scopes, raw_security = security_for_operation(operation)
excel_method = val(excel_row, ["Method"])
excel_endpoint = val(excel_row, ["Endpoint"])
excel_operation_id = val(excel_row, ["Operation ID"])
excel_summary = val(excel_row, ["Summary"])
excel_auth = val(excel_row, ["Authentication Scheme"])
excel_scopes = val(excel_row, ["OAuth Scope(s) / Access", "OAuth Scopes", "Scopes"])
excel_success = val(excel_row, ["Success Response(s)", "Success Response", "Status Code"])

st.header("4. Documentation comparison")
left, right = st.columns(2)
with left:
    st.subheader("OpenAPI JSON")
    st.write("**Summary:**", operation.get("summary", "(not specified)"))
    st.write("**Operation ID:**", operation.get("operationId", "(not specified)"))
    st.write("**Security scheme:**", oas_auth)
    st.write("**Scopes in security requirement:**", ", ".join(oas_scopes) if oas_scopes else "(none / not declared)")
    st.write("**Responses documented:**", ", ".join(operation.get("responses", {}).keys()) or "(none)")
    st.write("**Request body:**", "Yes" if "requestBody" in operation else "No")
with right:
    st.subheader("Excel matrix")
    if excel_row:
        st.write("**Worksheet:**", excel_sheet)
        st.write("**Summary:**", excel_summary or "(not specified)")
        st.write("**Operation ID:**", excel_operation_id or "(not specified)")
        st.write("**Authentication scheme:**", excel_auth or "(not specified)")
        st.write("**OAuth scopes/access:**", excel_scopes or "(none / not declared)")
        st.write("**Success response(s):**", excel_success or "(not specified)")
    else:
        st.warning(f"Excel matrix: {excel_sheet}")

oas_scope_set = set(oas_scopes)
excel_scope_set = {s.strip() for s in re.split(r"[,;]", excel_scopes) if s.strip() and s.strip().lower() not in {"(no scopes)", "not declared"}}
checks = []
checks.append(("Operation exists in OpenAPI", True, "Exact method/path match"))
checks.append(("Endpoint exists in Excel matrix", bool(excel_row), excel_sheet))
if excel_row:
    checks.append(("Operation ID matches", not excel_operation_id or excel_operation_id == operation.get("operationId", ""), f"OpenAPI={operation.get('operationId','')}; Excel={excel_operation_id}"))
    checks.append(("Authentication scheme matches", (not excel_auth) or (excel_auth.lower() in oas_auth.lower() or oas_auth.lower() in excel_auth.lower()), f"OpenAPI={oas_auth}; Excel={excel_auth}"))
    checks.append(("Scope declarations match", (oas_scope_set == excel_scope_set) if (oas_scope_set or excel_scope_set) else True, f"OpenAPI={sorted(oas_scope_set)}; Excel={sorted(excel_scope_set)}"))
comparison_df = pd.DataFrame([{"Check": a, "Result": "MATCH" if b else "MISMATCH / REVIEW", "Details": c} for a,b,c in checks])
st.dataframe(comparison_df, use_container_width=True, hide_index=True)
static_ok = all(c[1] for c in checks)
if static_ok:
    st.success("Static comparison: the endpoint declarations match across the loaded sources.")
else:
    st.warning("Static comparison: one or more declarations need review. Check the details above.")

with st.expander("OpenAPI security requirement (raw)", expanded=False):
    st.json(raw_security if raw_security is not None else {"security": "not declared at operation level"})
with st.expander("Response definitions (raw)", expanded=False):
    st.json(operation.get("responses", {}))

st.header("5. Runtime test with all three tokens")
st.warning("This sends real HTTP requests to the Faspex host. GET/HEAD/OPTIONS are treated as read-only. Other methods require explicit confirmation and may change or delete data.")
tokens = {}
t1, t2, t3 = st.columns(3)
with t1:
    tokens["user"] = st.text_input("User Bearer token", type="password", key="user_token")
with t2:
    tokens["manager"] = st.text_input("Manager Bearer token", type="password", key="manager_token")
with t3:
    tokens["admin"] = st.text_input("Admin Bearer token", type="password", key="admin_token")

# Generate request inputs from the selected OpenAPI operation. Generated values are editable examples,
# not guaranteed valid resource IDs or business fixtures.
path_item = paths.get(canonical_path, {})
params_meta = operation_parameters(oas, path_item, operation)
path_params = [p for p in params_meta if p.get("in") == "path"]
query_params = [p for p in params_meta if p.get("in") == "query"]
path_values = {}
if path_params:
    st.subheader("Required path parameters")
    st.caption("Values are taken from OpenAPI examples/enums where available. Replace any REPLACE_WITH_* value with a real ID from your test environment.")
    pcols = st.columns(min(3, len(path_params)))
    for i, param in enumerate(path_params):
        pname = param.get("name", f"path_param_{i}")
        schema = resolve_ref(oas, param.get("schema", {}))
        default = schema_example(oas, schema, pname)
        if isinstance(param.get("example"), (str, int, float, bool)):
            default = param["example"]
        elif param.get("examples"):
            first_ex = next(iter(param["examples"].values()))
            first_ex = resolve_ref(oas, first_ex)
            if isinstance(first_ex, dict) and "value" in first_ex: default = first_ex["value"]
        with pcols[i % len(pcols)]:
            path_values[pname] = st.text_input(f"{pname}{' *' if param.get('required', False) else ''}", value=str(default), key=f"path_{method}_{canonical_path}_{pname}")

query_defaults = {}
for param in query_params:
    pname = param.get("name", "")
    schema = resolve_ref(oas, param.get("schema", {}))
    if "example" in param: default = param["example"]
    elif "examples" in param and param["examples"]:
        ex = resolve_ref(oas, next(iter(param["examples"].values())))
        default = ex.get("value", schema_example(oas, schema, pname)) if isinstance(ex, dict) else schema_example(oas, schema, pname)
    else: default = schema_example(oas, schema, pname)
    if param.get("required") or (isinstance(default, str) and default.startswith("REPLACE_WITH_")):
        query_defaults[pname] = default
    elif "example" in param or "default" in schema or "enum" in schema:
        query_defaults[pname] = default

body_schema, body_required, body_examples = request_body_schema(oas, operation)
if body_examples:
    body_default = body_examples[0]
else:
    body_default = schema_example(oas, body_schema, "body") if body_schema else {}
if not body_schema and not operation.get("requestBody"):
    body_default = {}

st.subheader("Query parameters")
st.caption("Required query parameters are prefilled from the API documentation. Replace REPLACE_WITH_* values; add/edit fields in JSON if needed.")
params_text = st.text_area("Query parameters (JSON object)", value=json.dumps(query_defaults, indent=2, ensure_ascii=False), height=120, key=f"query_{method}_{canonical_path}")
headers_text = st.text_area("Additional headers (optional JSON object)", value="{}", height=70)
if operation.get("requestBody"):
    st.subheader("Request body")
    st.caption("Generated from the OpenAPI request-body schema/example. Required body properties are included when documented; replace placeholders with valid test data.")
    body_text = st.text_area(f"Request body (JSON){' — required' if body_required else ''}", value=json.dumps(body_default, indent=2, ensure_ascii=False), height=220, key=f"body_{method}_{canonical_path}")
else:
    body_text = st.text_area("Request body (JSON; this operation does not document a request body)", value="", height=80)
allow_mutation = st.checkbox("I understand this non-read-only request may modify or delete data", value=False)
show_body = st.checkbox("Include response body in the local results (may contain sensitive configuration)", value=False)
run_button = st.button("Run documentation + runtime validation", type="primary", use_container_width=True)

def parse_json_object(raw, label):
    try:
        obj = json.loads(raw or "{}")
        if not isinstance(obj, dict):
            raise ValueError("must be a JSON object")
        return obj
    except Exception as e:
        raise ValueError(f"{label}: {e}")

if run_button:
    if not base_url.startswith("https://") and not base_url.startswith("http://"):
        st.error("Base host must start with https:// or http://.")
        st.stop()
    if not any(tokens.values()):
        st.error("Enter at least one token. Empty tokens are skipped.")
        st.stop()
    read_only = method in ("GET", "HEAD", "OPTIONS")
    if not read_only and not allow_mutation:
        st.error("Non-read-only method blocked. Check the endpoint, prepare safe test data/cleanup, then tick the confirmation box to continue.")
        st.stop()
    resolved_path = canonical_path
    missing_path_values = []
    for param in path_params:
        pname = param.get("name", "")
        value = str(path_values.get(pname, "")).strip()
        if not value or value.startswith("REPLACE_WITH_"):
            missing_path_values.append(pname)
        resolved_path = resolved_path.replace("{" + pname + "}", quote(value, safe=""))
    if missing_path_values:
        st.error("Enter real values for required path parameter(s): " + ", ".join(missing_path_values) + ". The OpenAPI file may not provide real resource IDs.")
        st.stop()
    try:
        params = parse_json_object(params_text, "Query parameters")
        extra_headers = parse_json_object(headers_text, "Additional headers")
        request_body = None
        if body_text.strip():
            request_body = json.loads(body_text)
    except Exception as e:
        st.error(str(e))
        st.stop()
    missing_query = [p.get("name") for p in query_params if p.get("required") and (p.get("name") not in params or params.get(p.get("name")) in (None, "", []) or (isinstance(params.get(p.get("name")), str) and params.get(p.get("name")).startswith("REPLACE_WITH_")))]
    if missing_query:
        st.error("Required query parameter(s) missing or still using a generated placeholder: " + ", ".join(missing_query))
        st.stop()
    def has_placeholder(value):
        if isinstance(value, str):
            return value.startswith("REPLACE_WITH_")
        if isinstance(value, dict):
            return any(has_placeholder(v) for v in value.values())
        if isinstance(value, list):
            return any(has_placeholder(v) for v in value)
        return False
    if body_required and has_placeholder(request_body):
        st.error("The required request body contains REPLACE_WITH_* placeholders. Replace them with valid values from your test data before running.")
        st.stop()
    if body_required and request_body is None:
        st.error("This operation requires a request body according to OpenAPI.")
        st.stop()
    url = base_url.rstrip("/") + resolved_path
    st.write("**Request URL:**", url)
    st.caption("The app never displays token values or saves them into the report. Avoid response-body capture if the endpoint returns sensitive configuration.")

    rows = []
    for role in ("user", "manager", "admin"):
        token = tokens.get(role, "").strip()
        if not token:
            rows.append({"Role": role.title(), "HTTP status": "SKIPPED", "Result": "No token entered", "Content-Type": "", "Duration (ms)": "", "Documentation expectation": "", "Verdict": "NOT TESTED"})
            continue
        req_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        for k, v in extra_headers.items():
            # Don't allow user-supplied auth to override the token field.
            if str(k).lower() != "authorization":
                req_headers[str(k)] = str(v)
        start = time.time()
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=req_headers,
                params=params,
                json=request_body,
                timeout=int(timeout_s),
                verify=verify_tls,
                allow_redirects=follow_redirects,
            )
            duration = round((time.time() - start) * 1000)
            code = response.status_code
            content_type = response.headers.get("Content-Type", "")
            allowed_roles = {x.lower() for x in oas_scopes}
            # Interpret scope names as allowed role classes, following the Excel matrix's "Scope(s) / Access" convention.
            # This is a heuristic, not a substitute for inspecting the actual token claims.
            if not oas_scopes:
                expectation = "Not inferable from operation-level OAuth scopes"
                verdict = "NEEDS REVIEW"
            elif role in allowed_roles:
                expectation = "Expected to be allowed (role listed)"
                if 200 <= code < 300:
                    verdict = "PASS — observed success"
                elif code in (401, 403):
                    verdict = "MISMATCH? role listed but access denied; inspect token/claims and server policy"
                else:
                    verdict = "INCONCLUSIVE — request/fixture/server response"
            else:
                expectation = "Expected to be denied (role not listed)"
                if code in (401, 403):
                    verdict = "PASS — observed denial"
                elif 200 <= code < 300:
                    verdict = "MISMATCH? role not listed but access succeeded; inspect token scopes and policy"
                else:
                    verdict = "INCONCLUSIVE — response does not establish authorization"
            body_preview = ""
            if show_body:
                try:
                    parsed = response.json()
                    body_preview = json.dumps(parsed, ensure_ascii=False)[:5000]
                except Exception:
                    body_preview = response.text[:5000]
            rows.append({
                "Role": role.title(),
                "HTTP status": code,
                "Result": status_class(code),
                "Content-Type": content_type,
                "Duration (ms)": duration,
                "Documentation expectation": expectation,
                "Verdict": verdict,
                "Response body (optional)": body_preview,
            })
        except requests.RequestException as e:
            rows.append({"Role": role.title(), "HTTP status": "ERROR", "Result": str(e), "Content-Type": "", "Duration (ms)": "", "Documentation expectation": "", "Verdict": "REQUEST FAILED"})
    result_df = pd.DataFrame(rows)
    st.subheader("Runtime results")
    st.dataframe(result_df, use_container_width=True, hide_index=True)
    st.info("Runtime verdicts are provisional: a 401/403 can be caused by invalid/expired tokens, while 400/404/422 often indicate request or fixture issues. The app uses the documented role names as a role-access heuristic; inspect actual token claims and server policy before filing a scope defect.")

    # Download a token-free report.
    safe_report = result_df.drop(columns=["Response body (optional)"], errors="ignore").copy()
    safe_report.insert(0, "Endpoint", f"{method} {canonical_path}")
    safe_report.insert(1, "Base host", base_url)
    safe_report.insert(2, "OpenAPI operationId", operation.get("operationId", ""))
    safe_report.insert(3, "OpenAPI scopes", ", ".join(oas_scopes))
    safe_report.insert(4, "Excel scopes", excel_scopes)
    safe_report.insert(5, "Static documentation match", "MATCH" if static_ok else "REVIEW")
    st.download_button(
        "Download token-free results CSV",
        data=safe_report.to_csv(index=False).encode("utf-8"),
        file_name="faspex_endpoint_validation_results.csv",
        mime="text/csv",
    )

st.divider()
st.caption("Local-use prototype. It does not automatically discover the UI screen, inspect UI permissions, or prove authorization semantics solely from HTTP status. Use test/staging data, especially for write/delete methods.")
