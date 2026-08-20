# -*- coding: utf-8 -*-
"""`probe()` — the difference between "configured" and "actually usable".

WHY THIS EXISTS
    An external review installed this on a blank machine and found the failure that this
    method is here to prevent:

        compose comes up · every container reports healthy · the settings screen shows a
        green tick next to embedding · and the FIRST REAL WRITE fails

    The bundled compose file starts an Ollama container and never pulls a model. Every
    check that existed asked "is embedding configured" — enabled, a model name, a
    plausible base URL — and all three were true. Nothing asked the only question that
    mattered: *can it actually produce a vector.*

    🔴 The cost is when the answer arrives. It turns up attached to whatever the person
       happened to be doing, long after the moment they were prepared to hear about
       configuration — and the settings screen, the one place built to answer "is this set
       up right", had already said yes.

WHY IT ASKS FOR A REAL VECTOR
    Querying the backend's model list would be asking a proxy for the question. Embedding
    one short string IS the question, and it is the only form that works the same against
    Ollama, an OpenAI-compatible API, or anything else, without this code needing to know
    which one it is talking to.
"""
import asyncio

import pytest

from core.embedding_engine import EmbeddingEngine


class Backend:
    """A stand-in for whatever is on the other end. Each mode is one real failure."""

    api_format = "ollama"
    base_url = "http://ollama:11434"

    def __init__(self, mode):
        self.mode = mode
        self.calls = 0

    def model_name(self):
        return "bge-m3"

    async def generate_async(self, text):
        self.calls += 1
        if self.mode == "model_missing":
            raise _NotFound('model "bge-m3" not found')
        if self.mode == "unreachable":
            await asyncio.sleep(5)
            return [0.1]
        if self.mode == "empty":
            return []
        return [0.1, 0.2, 0.3]


class _NotFound(Exception):
    status_code = 404


def engine(mode="ok", enabled=True):
    e = EmbeddingEngine.__new__(EmbeddingEngine)
    e.enabled = enabled
    e._backend = Backend(mode) if enabled else None
    e.model = "bge-m3"
    e._query_cache = {}
    return e


def run(coro):
    return asyncio.run(coro)


def test_a_working_backend_reports_working():
    ok, why = run(engine("ok").probe())
    assert ok is True and why == ""


def test_a_model_that_was_never_pulled_is_reported_as_such():
    # Criterion: THE case this method was added for. It must not merely fail — it must say
    # the thing the person has to go and do.
    ok, why = run(engine("model_missing").probe())
    assert ok is False
    assert "bge-m3" in why
    assert "Ollama" in why or "ollama" in why


def test_a_backend_that_never_answers_is_reported_with_the_model_name():
    # Criterion: "no answer" and "wrong model" arrive at the same place — the settings
    # screen — so the message has to name both possibilities rather than say "timeout".
    ok, why = run(engine("unreachable").probe(timeout_seconds=0.2))
    assert ok is False
    assert "bge-m3" in why
    assert "0.2" in why, "the timeout it actually waited should be visible, not rounded to 0"


def test_an_empty_vector_counts_as_broken():
    # Criterion: a backend that answers with nothing is the most dangerous shape of all —
    # it raises nothing, so every caller treats it as "this text has no vector" rather than
    # "the engine is not working".
    ok, why = run(engine("empty").probe())
    assert ok is False and "empty" in why


def test_switched_off_is_not_reported_as_broken():
    # Criterion: choosing not to use embedding is a decision, not a fault. Reporting it as
    # a failure would put a red mark on a perfectly deliberate setup, and a screen that
    # cries wolf stops being read.
    ok, why = run(engine(enabled=False).probe())
    assert ok is False
    assert "off" in why.lower()


def test_the_probe_costs_exactly_one_call():
    # Criterion: it runs on a screen someone opens repeatedly. One short string, once.
    e = engine("ok")
    run(e.probe())
    assert e._backend.calls == 1


class _Unexpected(Exception):
    """Something no one wrote a branch for."""


def test_any_ordinary_failure_comes_back_as_an_answer():
    # Criterion: it is called from a page render. Whatever the backend does — including
    # something nobody anticipated — this has to come back with an answer rather than take
    # the settings screen down with it.
    class Exploding:
        api_format = "ollama"
        base_url = ""

        def model_name(self):
            return "bge-m3"

        async def generate_async(self, text):
            raise _Unexpected("something no one wrote a branch for")

    e = engine("ok")
    e._backend = Exploding()
    ok, why = run(e.probe())
    assert ok is False
    assert "no one wrote a branch for" in why


def test_an_interrupt_is_deliberately_not_swallowed():
    # Criterion: the limit of the rule above, stated so nobody "fixes" it by widening the
    # catch to BaseException. Ctrl-C and a shutdown are not backend failures — swallowing
    # them would turn "stop the server" into "the settings screen says embedding is broken"
    # while the process keeps running.
    class Interrupted:
        api_format = "ollama"
        base_url = ""

        def model_name(self):
            return "bge-m3"

        async def generate_async(self, text):
            raise KeyboardInterrupt

    e = engine("ok")
    e._backend = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        run(e.probe())
