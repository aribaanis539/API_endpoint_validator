import base64
import json
from io import BytesIO
from PIL import Image

# Path to the generated image
image_path = "a_clean_minimal_professional_looking_digital_ima.png"

# Open and convert the image to JPEG
image = Image.open(image_path).convert("RGB")

buffer = BytesIO()
image.save(buffer, format="JPEG", quality=90)

# Encode the JPEG image as Base64
encoded_image = base64.b64encode(buffer.getvalue()).decode("utf-8")

# Create the requested JSON structure
result = {
    "file_contents": encoded_image,
    "file_name": "foobar.jpg"
}

print(json.dumps(result, indent=2))