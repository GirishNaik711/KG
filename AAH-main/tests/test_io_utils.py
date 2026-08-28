"""Tests for aah.core.common.io_utils."""

import json
from pathlib import Path

import yaml
import pytest

from aah.core.common.io_utils import (
    append_jsonl,
    ensure_dir,
    read_json,
    read_jsonl,
    read_text,
    read_yaml,
    render_template_string,
    write_json,
    write_text,
    write_yaml,
)


class TestYamlIO:
    def test_write_and_read_yaml(self, tmp_path):
        path = tmp_path / "test.yaml"
        data = {"key": "value", "nested": {"a": 1, "b": [1, 2, 3]}}
        write_yaml(data, path)
        result = read_yaml(path)
        assert result == data

    def test_read_yaml_empty_file(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        result = read_yaml(path)
        assert result == {}

    def test_write_yaml_creates_parents(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "test.yaml"
        write_yaml({"key": "val"}, path)
        assert path.exists()
        assert read_yaml(path) == {"key": "val"}


class TestJsonIO:
    def test_write_and_read_json_dict(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"key": "value", "list": [1, 2, 3]}
        write_json(data, path)
        result = read_json(path)
        assert result == data

    def test_write_and_read_json_list(self, tmp_path):
        path = tmp_path / "test.json"
        data = [{"id": 1}, {"id": 2}]
        write_json(data, path)
        result = read_json(path)
        assert result == data

    def test_write_json_creates_parents(self, tmp_path):
        path = tmp_path / "a" / "b" / "test.json"
        write_json({"x": 1}, path)
        assert path.exists()


class TestJsonlIO:
    def test_append_and_read_jsonl(self, tmp_path):
        path = tmp_path / "test.jsonl"
        append_jsonl({"a": 1}, path)
        append_jsonl({"b": 2}, path)
        append_jsonl({"c": 3}, path)
        records = read_jsonl(path)
        assert len(records) == 3
        assert records[0] == {"a": 1}
        assert records[2] == {"c": 3}

    def test_read_jsonl_nonexistent(self, tmp_path):
        path = tmp_path / "nonexistent.jsonl"
        assert read_jsonl(path) == []

    def test_read_jsonl_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        assert read_jsonl(path) == []


class TestTextIO:
    def test_write_and_read_text(self, tmp_path):
        path = tmp_path / "test.txt"
        write_text("hello world\n", path)
        assert read_text(path) == "hello world\n"

    def test_write_text_creates_parents(self, tmp_path):
        path = tmp_path / "a" / "b" / "test.txt"
        write_text("content", path)
        assert path.exists()


class TestTemplateRendering:
    def test_render_template_string(self):
        template = "Hello $name, welcome to $project!"
        result = render_template_string(template, {"name": "Alice", "project": "RAPIDS"})
        assert result == "Hello Alice, welcome to AAH!"

    def test_render_template_missing_var_safe(self):
        template = "Hello $name, your ID is $id"
        result = render_template_string(template, {"name": "Bob"})
        assert result == "Hello Bob, your ID is $id"


class TestEnsureDir:
    def test_creates_nested_dirs(self, tmp_path):
        path = tmp_path / "a" / "b" / "c"
        result = ensure_dir(path)
        assert path.is_dir()
        assert result == path

    def test_idempotent(self, tmp_path):
        path = tmp_path / "existing"
        path.mkdir()
        ensure_dir(path)  # Should not raise
        assert path.is_dir()
