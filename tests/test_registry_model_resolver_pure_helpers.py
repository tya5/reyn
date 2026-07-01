"""Tier 2: pure helpers in source_resolver.py and model_resolver.py.

  source_resolver._docker_image_name(image)   — short name from Docker image ref
  model_resolver._spec_to_dict(spec)           — ModelSpec → flat dict
  model_resolver._strip_keys(d, keys)          — dict without specified keys
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.registry.source_resolver import _docker_image_name
from reyn.llm.model_resolver import ModelSpec, _spec_to_dict, _strip_keys

# ---------------------------------------------------------------------------
# _docker_image_name
# ---------------------------------------------------------------------------


def test_docker_image_name_simple_name() -> None:
    """Tier 2: plain name with no tag or path → returned as-is."""
    assert _docker_image_name("myimage") == "myimage"


def test_docker_image_name_strips_tag() -> None:
    """Tier 2: tag after ':' is stripped."""
    assert _docker_image_name("myimage:latest") == "myimage"


def test_docker_image_name_strips_repo_path() -> None:
    """Tier 2: registry/repo prefix before '/' is stripped."""
    assert _docker_image_name("registry.io/myorg/myimage") == "myimage"


def test_docker_image_name_strips_tag_and_repo() -> None:
    """Tier 2: both registry prefix and tag are stripped."""
    assert _docker_image_name("registry.io/myorg/myimage:v1.0") == "myimage"


def test_docker_image_name_official_image_with_tag() -> None:
    """Tier 2: official Docker Hub image with tag → image name only."""
    assert _docker_image_name("ubuntu:22.04") == "ubuntu"


# ---------------------------------------------------------------------------
# _spec_to_dict
# ---------------------------------------------------------------------------


def test_spec_to_dict_model_only() -> None:
    """Tier 2: ModelSpec with model only → {'model': ...}."""
    spec = ModelSpec(model="claude-3-opus")
    result = _spec_to_dict(spec)
    assert result["model"] == "claude-3-opus"


def test_spec_to_dict_includes_kwargs() -> None:
    """Tier 2: ModelSpec kwargs are merged into the output dict."""
    spec = ModelSpec(model="claude-3-sonnet", kwargs={"temperature": 0.5, "max_tokens": 512})
    result = _spec_to_dict(spec)
    assert result["model"] == "claude-3-sonnet"
    assert result["temperature"] == 0.5
    assert result["max_tokens"] == 512


def test_spec_to_dict_empty_kwargs() -> None:
    """Tier 2: empty kwargs dict → result contains only 'model' key."""
    spec = ModelSpec(model="gpt-4", kwargs={})
    result = _spec_to_dict(spec)
    assert "model" in result
    assert result["model"] == "gpt-4"


def test_spec_to_dict_kwargs_do_not_override_model_key() -> None:
    """Tier 2: 'model' in kwargs would shadow the spec.model — result is deterministic."""
    spec = ModelSpec(model="real-model", kwargs={"foo": "bar"})
    result = _spec_to_dict(spec)
    assert result["model"] == "real-model"
    assert result["foo"] == "bar"


# ---------------------------------------------------------------------------
# _strip_keys
# ---------------------------------------------------------------------------


def test_strip_keys_removes_specified() -> None:
    """Tier 2: keys in the removal set are absent from the result."""
    d = {"a": 1, "b": 2, "c": 3}
    result = _strip_keys(d, {"b", "c"})
    assert "b" not in result
    assert "c" not in result
    assert result["a"] == 1


def test_strip_keys_missing_key_is_noop() -> None:
    """Tier 2: a key in the removal set that isn't in d is silently ignored."""
    d = {"a": 1}
    result = _strip_keys(d, {"b", "c"})
    assert result == {"a": 1}


def test_strip_keys_empty_removal_set() -> None:
    """Tier 2: empty removal set returns a copy of the original dict."""
    d = {"x": 10, "y": 20}
    result = _strip_keys(d, set())
    assert result == {"x": 10, "y": 20}


def test_strip_keys_does_not_mutate_original() -> None:
    """Tier 2: the original dict is not modified by the operation."""
    d = {"a": 1, "b": 2}
    _strip_keys(d, {"a"})
    assert "a" in d


def test_strip_keys_all_keys_removed() -> None:
    """Tier 2: removing all keys returns an empty dict."""
    d = {"a": 1, "b": 2}
    result = _strip_keys(d, {"a", "b"})
    assert result == {}
