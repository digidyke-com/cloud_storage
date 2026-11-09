# Copyright (c) 2026, AgriTheory and contributors
# For license information, please see license.txt

import base64
from pathlib import Path
from unittest.mock import MagicMock

import frappe
import pytest

from cloud_storage.cloud_storage.overrides.file import CloudStorageFile, get_cloud_storage_client


TEST_FILES = Path(__file__).parent / "fixtures"


def _create_file_doc(file_name, file_url=None, is_private=0):
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"file_url": file_url or f"/files/{file_name}",
			"is_private": is_private,
		}
	)

	doc.__class__ = CloudStorageFile
	return doc


@pytest.mark.parametrize("ext", ["doc", "docx"])
def test_doc_preview_content(monkeypatch, ext):
	"""Test preview for doc/docx files using get_content"""

	fake_content = b"fake document content"

	doc = _create_file_doc(f"sample.{ext}")

	monkeypatch.setattr(
		"builtins.open",
		lambda *args, **kwargs: MagicMock(read=lambda: fake_content),
	)

	content = doc.get_content()

	assert content is not None


def _fake_libreoffice(calls):
	"""subprocess.run stand-in: records the command and writes a PDF where LibreOffice would."""

	def run(cmd, **kwargs):
		calls.append((cmd, kwargs))
		outdir = Path(cmd[cmd.index("--outdir") + 1])
		(outdir / (Path(cmd[-1]).stem + ".pdf")).write_bytes(b"%PDF fake")

	return run


@pytest.mark.parametrize("ext", ["ppt", "pptx", "odp", "key"])
def test_local_presentation_pdf_preview(monkeypatch, ext):
	"""Local presentation is converted in an isolated temp dir that is removed afterwards"""
	calls = []
	monkeypatch.setattr("cloud_storage.cloud_storage.overrides.file.subprocess.run", _fake_libreoffice(calls))
	doc = _create_file_doc(f"slides.{ext}")
	monkeypatch.setattr(CloudStorageFile, "get_content", lambda self, encodings=None: b"fake presentation")

	result = doc.get_pdf_preview()

	assert base64.b64decode(result).startswith(b"%PDF")
	cmd, kwargs = calls[0]
	assert cmd[:2] == ["timeout", "-k"] and "--headless" in cmd and kwargs["timeout"] > 0
	assert any(arg.startswith("-env:UserInstallation=") for arg in cmd)
	assert not Path(cmd[-1]).parent.exists()  # temp dir cleaned up


@pytest.mark.parametrize("ext", ["ppt", "pptx", "odp", "key"])
def test_s3_presentation_preview(monkeypatch, ext):
	"""Preview of a file stored in S3 converts the object's bytes"""
	pres_content = b"presentation binary"
	client = get_cloud_storage_client()
	client.put_object(Bucket=client.bucket, Key=f"slides.{ext}", Body=pres_content)

	seen = []

	def run(cmd, **kwargs):
		seen.append(Path(cmd[-1]).read_bytes())
		_fake_libreoffice([])(cmd, **kwargs)

	monkeypatch.setattr("cloud_storage.cloud_storage.overrides.file.subprocess.run", run)
	doc = _create_file_doc(f"slides.{ext}", file_url=f"/api/method/retrieve?key=slides.{ext}")
	doc.s3_key = f"slides.{ext}"

	result = doc.get_pdf_preview()

	assert base64.b64decode(result).startswith(b"%PDF")
	assert seen == [pres_content]


def test_preview_conversion_failure_is_reported(monkeypatch):
	import subprocess

	def run(cmd, **kwargs):
		raise subprocess.CalledProcessError(124, cmd, stderr=b"timed out")

	monkeypatch.setattr("cloud_storage.cloud_storage.overrides.file.subprocess.run", run)
	doc = _create_file_doc("slides.pptx")
	monkeypatch.setattr(CloudStorageFile, "get_content", lambda self, encodings=None: b"x")

	with pytest.raises(frappe.ValidationError):
		doc.get_pdf_preview()


def test_preview_folder_error():
	doc = _create_file_doc("folder")
	doc.is_folder = 1

	with pytest.raises(frappe.ValidationError):
		doc.get_pdf_preview()


def test_safe_path():
	from cloud_storage.cloud_storage.overrides.file import is_safe_path

	safe = frappe.get_site_path("public", "files", "test.txt")

	assert is_safe_path(safe)


def test_generate_sharing_link(monkeypatch):

	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": "sample.txt",
			"is_private": 0,
		}
	).insert()

	from cloud_storage.cloud_storage.overrides.file import get_sharing_link

	url = get_sharing_link(doc.name)

	assert "share?key=" in url
