import base64

from gemini_api import generate_image


class FakeResponse:
    status_code = 200
    content = b"response"

    def json(self):
        raw = b"jpeg-bytes"
        return {
            "id": "int_img_jpeg",
            "status": "completed",
            "model": "gemini-3.1-flash-lite-image",
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "image",
                            "mime_type": "image/jpeg",
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    ],
                }
            ],
        }


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def test_generate_image_requests_jpeg_response_format():
    client = FakeHTTP()
    result = generate_image(
        prompt="create a branded social creative",
        api_key="secret",
        model="gemini-3.1-flash-lite-image",
        aspect_ratio="4:5",
        image_size="1K",
        http_client=client,
    )

    payload = client.calls[0][1]["json"]
    assert payload["response_format"]["mime_type"] == "image/jpeg"
    assert result.mime_type == "image/jpeg"
    assert result.image_bytes == b"jpeg-bytes"
