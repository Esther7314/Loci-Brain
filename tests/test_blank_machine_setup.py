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
from urllib.parse import urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
DEFAULT_CONFIG = yaml.safe_load((ROOT / "config.default.yaml").read_text(encoding="utf-8"))

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


def test_the_pull_names_the_model_the_default_configures():
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

    configured = DEFAULT_CONFIG["embedding"]["model"]
    assert configured in pulled, (
        f"config.default.yaml configures {configured!r}, but what actually gets pulled is "
        f"{pulled} — the stack would come up and fail on the first write")


def test_the_pull_comes_with_ollama_and_only_with_it():
    # Criterion: Ollama is opt-in (a profile). The pull has to share that profile: without
    # it a plain `up` would wait on a container that never starts, and with a different
    # one `--profile ollama` would bring up an Ollama with no model in it.
    puller = next(svc for svc in SERVICES.values()
                  if "pull" in " ".join(str(v) for v in (svc.get("command") or [])))
    assert SERVICES["ollama"].get("profiles") and puller.get("profiles") == SERVICES["ollama"]["profiles"]
    dep = SERVICES["loci-brain"].get("depends_on") or {}
    assert not isinstance(dep, list) and dep.get("ollama", {}).get("required") is False, (
        "loci-brain must not require ollama: a plain `docker compose up` leaves it out")


def test_the_pull_runs_once_rather_than_restarting_forever():
    # Criterion: it is a one-shot job. Left on the default restart policy it would exit,
    # be restarted, pull again, exit — a loop that looks like a crash loop in every
    # dashboard while actually being fine, which trains people to ignore the dashboard.
    puller = next(svc for name, svc in SERVICES.items()
                  if "pull" in " ".join(str(v) for v in (svc.get("command") or [])))
    assert str(puller.get("restart", "")).strip('"') == "no"


def test_the_keys_are_generated_where_compose_keeps_them():
    # Criterion: the bridge's key used to be in no file anybody copies, and following the
    # advice to lock the panel silently stopped dreams arriving. Now the image runs the
    # package launcher, which generates every key on the first start into the data
    # directory — so that directory has to be a volume, or the keys (and the pairing with
    # the gateway written into config.yaml) would be regenerated on every new container.
    entry = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert "package/launcher.py" in entry
    launcher = (ROOT / "package" / "launcher.py").read_text(encoding="utf-8")
    for key in ("LOCI_HOOK_TOKEN", "LOCI_HOST_TOKEN_GATEWAY", "LOCI_FETCH_TOKEN_GATEWAY",
                "LOCI_GATEWAY_PASSPHRASE"):
        assert f'"{key}"' in launcher.split("KEYS = (", 1)[1].split(")", 1)[0], key
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "LOCI_DATA_DIR=/app/data" in dockerfile
    targets = [str(v).split(":")[-1] for v in SERVICES["loci-brain"].get("volumes") or []]
    assert "/app/data" in targets and "/app/buckets" in targets


@pytest.mark.parametrize("key", ["LOCI_COMPRESS_API_KEY", "LOCI_EMBED_BASE_URL",
                                 "LOCI_EMBED_MODEL"])
def test_the_env_example_still_carries_the_required_keys(key):
    # Criterion: these are what "required" means for a first install. If one is dropped,
    # the failure is again a stack that starts and does not work.
    assert key in ENV_EXAMPLE


# ── The shipped default configuration ──────────────────────────────────────────
# Same shape of failure as everything above: a file handed to a new user that promises
# one thing while the README and the env example promise the opposite. The default
# pointed embedding at a Google endpoint, so anybody who did not also copy .env.example
# got a stack that quietly billed a network round-trip on every single recall.

def _default_embedding_host() -> str:
    base_url = str(DEFAULT_CONFIG.get("embedding", {}).get("base_url", ""))
    return urlparse(base_url).hostname or ""


def test_the_default_embedding_does_not_point_at_a_cloud_host():
    # Criterion: THE assertion. Not "is it exactly ollama" — that would go red on a
    # perfectly good rename — but "does the shipped default send every recall off this
    # machine". A loopback / compose-network hostname is local; anything with a public
    # suffix is not, and neither is anything reached over TLS.
    host = _default_embedding_host()
    assert host, "config.default.yaml no longer sets embedding.base_url"
    assert "." not in host, (
        f"the shipped default sends every embedding request to {host!r}. README says "
        "本地 is strongly recommended and .env.example configures a local endpoint; a "
        "default that disagrees means anybody who skips the env file pays per recall.")
    assert not str(DEFAULT_CONFIG["embedding"]["base_url"]).startswith("https://"), (
        "an https default embedding endpoint is by definition not on this machine")


def test_the_default_embedding_model_is_the_one_the_env_example_configures():
    # Criterion: pointing at the local endpoint with a model name that endpoint has never
    # heard of fails on the first write — the same invisible failure as the missing pull.
    configured = [line.split("=", 1)[1].split("#", 1)[0].strip()
                  for line in ENV_EXAMPLE.splitlines()
                  if line.lstrip("# ").startswith("LOCI_EMBED_MODEL=")]
    assert configured, ".env.example no longer names LOCI_EMBED_MODEL"
    assert DEFAULT_CONFIG["embedding"]["model"] == configured[0], (
        f"config.default.yaml defaults to {DEFAULT_CONFIG['embedding']['model']!r} while "
        f".env.example configures {configured[0]!r} — the two files handed to a new user "
        "disagree about what gets vectorised with")
