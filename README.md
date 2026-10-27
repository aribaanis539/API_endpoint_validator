# Faspex 5 Endpoint & OAuth Validator

A local Streamlit application that lets you select or enter a Faspex API endpoint, compare its OpenAPI JSON definition with the Excel scope matrix, then optionally test it with User, Manager, and Admin Bearer tokens.

## Included source documentation
- `openapi (8).json`
- `Faspex_5_API_Endpoint_OAuth_Matrix(1).xlsx`

These are copies of the files supplied for this task. You can also upload newer files in the app.

## Run on macOS

```bash
cd faspex_endpoint_validator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Streamlit opens a local browser page (usually `http://localhost:8501`).

## Usage
1. Confirm the bundled OpenAPI and Excel files are loaded, or upload newer versions in the sidebar.
2. Enter the base host. Default: `https://faspex-5018-ariba.dev.fyre.ibm.com`.
3. Select the HTTP method and choose a documented path, or type a path exactly.
4. Review the static comparison: operation ID, authentication scheme, and scope declarations.
5. The app reads the selected operation's OpenAPI `parameters`, path parameters, and `requestBody` schema. It pre-fills required query parameters and documented examples/defaults/enums, builds a JSON body from documented examples or schema properties, and prompts for path-parameter values.
6. Replace any `REPLACE_WITH_*` placeholders with real values from your test environment. Documentation often cannot provide real package, user, group, or other resource IDs. The app blocks unresolved required path/query values and required body placeholders.
7. Enter the three Bearer tokens into the password fields. Tokens are not displayed, included in the report, or persisted by this app.
8. Run the request. Empty token fields are skipped.
9. Download the token-free CSV report.

## Safety
- Start with GET requests.
- POST/PUT/PATCH/DELETE are blocked unless you explicitly confirm you understand they may mutate data.
- Before enabling write tests, prepare valid test fixtures and a verified cleanup/restoration plan.
- Do not use production credentials or paste tokens into chat/tickets/source control.
- TLS certificate verification is enabled by default.
- Response body capture is off by default because configuration endpoints may return sensitive data.
- The app sends requests only to the configured base host, plus the path you enter. Use only hosts you are authorized to test.

## Important interpretation notes
- Static comparison reports what the OpenAPI and Excel files declare; it does not independently prove the server implementation is correct.
- For runtime results, `200-299` is observed success and `401/403` is observed denial. Other statuses are generally inconclusive for scope enforcement.
- The runtime role verdict uses the documented scope names as an *expected role-access heuristic*, following the matrix's “OAuth Scope(s) / Access” column. OpenAPI security requirement arrays are formally lists of required OAuth scopes, so the app does not claim this heuristic is definitive. Confirm the actual token claims and Faspex authorization policy before treating a result as a defect.
- The app does not automate UI navigation. It can validate the API/documentation and help inform separate manual UI checks.

## Troubleshooting
- `ModuleNotFoundError`: activate the virtual environment and run `python -m pip install -r requirements.txt`.
- TLS errors: keep certificate verification enabled when possible; install the trusted internal CA rather than disabling verification if you can.
- `400` with a missing-parameter message: check the generated query/body JSON and the operation's documented parameters. If the server requires a parameter that is absent from the OpenAPI file, add it manually in the JSON editor; the app cannot infer undocumented requirements.
- `404`: confirm base host and whether the path includes `/aspera/faspex/api/v5`.
- `401`: check token expiry and audience/issuer.
- `403`: check actual token scope claims and role assignment.
