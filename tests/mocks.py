import json
from collections import namedtuple

from azure.cognitiveservices.speech import ResultReason
from azure.core.credentials_async import AsyncTokenCredential

MockToken = namedtuple("MockToken", ["token", "expires_on", "value"])


class MockAzureCredential(AsyncTokenCredential):

    async def get_token(self, *scopes, **kwargs):
        return MockToken("mock-token", 9999999999, "mock-token")


class MockAzureCredentialExpired(AsyncTokenCredential):

    def __init__(self):
        self.access_number = 0

    async def get_token(self, *scopes, **kwargs):
        self.access_number += 1
        if self.access_number == 1:
            return MockToken("", 0, "")
        else:
            return MockToken("", 9999999999, "")


class MockAsyncPageIterator:
    def __init__(self, data):
        self.data = data

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.data:
            raise StopAsyncIteration
        return self.data.pop(0)


class MockResponse:
    def __init__(self, status, text=None, headers=None):
        self._text = text or ""
        self.status = status
        self.headers = headers or {}

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def __aenter__(self):
        return self

    async def text(self):
        return self._text

    async def json(self):
        return json.loads(self._text)

    def raise_for_status(self):
        if self.status != 200:
            raise Exception(f"HTTP status {self.status}")


class MockAudio:
    def __init__(self, audio_data):
        self.audio_data = audio_data
        self.reason = ResultReason.SynthesizingAudioCompleted

    def read(self):
        return self.audio_data


class MockSpeechSynthesisCancellationDetails:
    def __init__(self):
        self.reason = "Canceled"
        self.error_details = "The synthesis was canceled."


class MockAudioCancelled:
    def __init__(self, audio_data):
        self.audio_data = audio_data
        self.reason = ResultReason.Canceled
        self.cancellation_details = MockSpeechSynthesisCancellationDetails()

    def read(self):
        return self.audio_data


class MockAudioFailure:
    def __init__(self, audio_data):
        self.audio_data = audio_data
        self.reason = ResultReason.NoMatch

    def read(self):
        return self.audio_data


class MockSynthesisResult:
    def __init__(self, result):
        self.__result = result

    def get(self):
        return self.__result


def mock_speak_text_success(self, text):
    return MockSynthesisResult(MockAudio("mock_audio_data"))


def mock_speak_text_cancelled(self, text):
    return MockSynthesisResult(MockAudioCancelled("mock_audio_data"))


def mock_speak_text_failed(self, text):
    return MockSynthesisResult(MockAudioFailure("mock_audio_data"))
