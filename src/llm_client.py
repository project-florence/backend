from openai import OpenAI


class LLM_client():
    client = None
    default_model: str = None

    def __init__(self, url, default_model=None, api_key="dummy_api_key"):
        client = OpenAI(
            api_key=api_key,
            base_url=url,
        )
        self.client = client
        self.default_model = default_model

    def get_response(self, prompt: str, role: str = "user", model: str = None):
        if model is None:
            if self.default_model is not None:
                model = self.default_model
            else:
                raise ValueError("No model provided")

        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": role, "content": prompt}
            ]
        )
        if response:
            return response.choices[0].message.content
        else:
            return None