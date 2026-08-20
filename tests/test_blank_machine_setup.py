# -*- coding: utf-8 -*-
"""What a blank machine needs, checked in the files that promise it.

WHY THIS FILE EXISTS
    An external review installed this from scratch and hit two things that were each
    invisible in exactly the same way:

      · compose started an Ollama container and **pulled no model**, so everything came up
        green and the first real write was what failed
      · the recommended setup says to put a password on the panel, and once it is on, the
        bridge needs a key that appeared in no example, no README and no error anyone was
        going to read — so dreams and nudges simply stopped arriving

    Neither is a crash. Both are the same shape: **an install that reports success and then
    does not work**, with the explanation arriving later and attached to the wrong thing.

    The fixes live in a compose file and an env example — files with no tests, which is
    precisely how they came to promise something they did not deliver. So the promises are
    asserted here.

WHAT THIS DOES NOT DO
    It does not start anything. It reads the files a new user is handed and checks they
    still say what they need to say. Whether Ollama then really pulls the model is the
    end-to-end suite's job.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

SERVICES = COMPOSE.get("services", {})


def test_the_compose_file_still_parses_and_has_services():
    # Criterion: a suite that silently checks an empty dict passes forever.
    assert len(SERVICES) >= 2


def test_something_pulls_the_embedding_model():
    # Criterion: THE assertion. Without it, a blank machine gets a healthy-looking stack
    # whose first real write fails — and the failure says nothing about a missing model.
    pullers = [name for name, svc in SERVICES.items()
               if "pull" in " ".join(str(v) for v in (svc.get("command") or []))]
    assert pullers, (
        "no service pulls an embedding model. A fresh `docker compose up` would bring "
        "everything up green and fail on the first write.")


def test_the_pull_names_the_model_the_env_example_configures():
    # Criterion: pulling *a* model is not enough — it has to be the one the configuration
    # points at. Two files each correct on their own and disagreeing with each other is
    # how this class of failure survives review in the first place.
    #
    # ⚠️ Matched against the `ollama pull …` invocation itself, not against the whole
    #    command block. The block also echoes the model name for the person watching the
    #    logs, and the mutation check caught this passing with the pull changed and only
    #    the echo left saying the right thing.
    import re
    commands = " ".join(" ".join(str(v) for v in (svc.get("command") or []))
                        for svc in SERVICES.values())
    pulled = re.findall(r"ollama\s+pull\s+(\S+)", commands)
    assert pulled, "no `ollama pull` invocation found in any service command"

    configured = [line.split("=", 1)[1].strip()
                  for line in ENV_EXAMPLE.splitlines()
                  if line.startswith("LOCI_EMBED_MODEL=")]
    assert configured, ".env.example no longer sets LOCI_EMBED_MODEL"
    assert configured[0] in pulled, (
        f".env.example configures {configured[0]!r}, but what actually gets pulled is "
        f"{pulled} — the stack would come up and fail on the first write")


def test_the_pull_runs_once_rather_than_restarting_forever():
    # Criterion: it is a one-shot job. Left on the default restart policy it would exit,
    # be restarted, pull again, exit — a loop that looks like a crash loop in every
    # dashboard while actually being fine, which trains people to ignore the dashboard.
    puller = next(svc for name, svc in SERVICES.items()
                  if "pull" in " ".join(str(v) for v in (svc.get("command") or [])))
    assert str(puller.get("restart", "")).strip('"') == "no"


def test_the_hook_token_is_in_the_env_example():
    # Criterion: the key exists in the code and worked; it was simply in no file anybody
    # copies. Following the recommended setup without it silently stops dreams arriving.
    assert "LOCI_HOOK_TOKEN" in ENV_EXAMPLE


def test_the_env_example_says_when_the_hook_token_becomes_necessary():
    # Criterion: a bare variable name is not enough. The trap is that it is unnecessary
    # right up until the moment someone follows the advice to lock the panel — so the
    # example has to name that moment, not just the variable.
    block = ENV_EXAMPLE.split("LOCI_HOOK_TOKEN")[0][-500:]
    assert "密码" in block or "锁" in block, (
        "LOCI_HOOK_TOKEN is listed but nothing says it becomes required once the panel "
        "is locked — which is the only situation in which its absence bites")


@pytest.mark.parametrize("key", ["LOCI_COMPRESS_API_KEY", "LOCI_EMBED_BASE_URL",
                                 "LOCI_EMBED_MODEL"])
def test_the_env_example_still_carries_the_required_keys(key):
    # Criterion: these are what "required" means for a first install. If one is dropped,
    # the failure is again a stack that starts and does not work.
    assert key in ENV_EXAMPLE
