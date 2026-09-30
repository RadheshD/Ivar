import pytest

from apps.ai.llm import FakeLLMClient
from apps.ai.pipeline import IntentPipeline
from apps.conversations.engine import ConversationEngine
from apps.conversations.fake_backend import FakeBackend
from apps.conversations.state import InMemorySessionStore

from .harness import Chat, Clock, FlakyBackend


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def flaky(backend):
    return FlakyBackend(backend)


@pytest.fixture
def llm():
    # No scripted replies: every LLM call fails, like an LLM that is down.
    return FakeLLMClient()


@pytest.fixture
def store(clock):
    return InMemorySessionStore(ttl_seconds=3600, clock=clock)


@pytest.fixture
def make_engine(store, llm, clock):
    def build(backend, **kwargs):
        pipeline = kwargs.pop("pipeline", None) or IntentPipeline(llm)
        return ConversationEngine(backend, store, pipeline, clock=clock, **kwargs)

    return build


@pytest.fixture
def engine(make_engine, flaky):
    return make_engine(flaky)


@pytest.fixture
def chat(engine):
    return Chat(engine)
