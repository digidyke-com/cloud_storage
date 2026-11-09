# Copyright (c) 2024, AgriTheory and contributors
# For license information, please see license.txt

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import types
import uuid
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
import tempfile

import frappe
from boto3.session import Session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from frappe import DoesNotExistError, _
from frappe.core.doctype.file.file import (
	FILE_ENCODING_OPTIONS,
	OLE_FILE_SIGNATURE,
	File,
	get_files_path,
)
from frappe.core.doctype.file.utils import get_content_hash, get_safe_file_name
from frappe.model.rename_doc import rename_doc
from frappe.utils import cint, get_datetime, get_url
from frappe.utils.image import optimize_image, strip_exif_data
from magic import from_buffer
from PIL import UnidentifiedImageError
from werkzeug.datastructures import FileStorage

FILE_URL = "/api/method/retrieve?key={path}"
URL_PREFIXES = ("http://", "https://", "/api/method/retrieve")


class CloudStorageFile(File):
	@File.is_remote_file.getter
	def is_remote_file(self) -> bool:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: is_remote_file
		"""
		if self.file_url:  # type: ignore
			return self.file_url.startswith(URL_PREFIXES)  # type: ignore
		return not self.content

	def validate(self) -> None:
		self.validate_cloud_object_access()
		if not self.is_folder:
			# core checks that must also apply to cloud uploads (which return early below)
			self.enforce_public_file_restrictions()
			if self.flags.cloud_storage:
				self.validate_attachment_references()
		# Dedup/association disabled: do not auto-associate on save (this was the
		# source of the infinite-recursion 500 between duplicate-hash rows).
		if self.flags.cloud_storage or self.flags.ignore_file_validate:
			return
		if not self.is_remote_file:
			self.custom_validate()
		else:
			self.validate_file_url()

	def validate_cloud_object_access(self):
		"""s3_key decides which bucket object retrieve() serves. Only the upload path assigns
		new keys; anyone else may only reuse the key of a File they can already read (as core's
		validate_private_file_access does for file_url), so a user can't point their own record
		at someone else's private object - or at a retained object of a deleted File."""
		if self.flags.cloud_storage or self.flags.ignore_permissions or not self.s3_key:
			return
		if not self.is_new() and not self.has_value_changed("s3_key"):
			return
		if frappe.session.user == "Administrator":
			return

		for name in frappe.get_all(
			"File", filters={"s3_key": self.s3_key, "name": ["!=", self.name or ""]}, pluck="name"
		):
			if frappe.has_permission("File", "read", doc=frappe.get_doc("File", name)):
				return

		frappe.throw(_("You do not have permission to access this file"), frappe.PermissionError)

	def custom_validate(self):
		if self.is_folder:
			return

		# Ensure correct formatting and type
		self.file_url = unquote(self.file_url) if self.file_url else ""

		self.validate_attachment_references()

		# when dict is passed to get_doc for creation of new_doc, is_new returns None
		# this case is handled inside handle_is_private_changed
		if not self.is_new() and self.has_value_changed("is_private"):
			self.handle_is_private_changed()

		self.validate_file_path()
		self.validate_file_url()

		config = frappe.conf.cloud_storage_settings
		if not config or config.get("use_local"):
			self.validate_file_on_disk()

		self.file_size = frappe.form_dict.file_size or self.file_size

	def save_file(
		self,
		content: bytes | str | None = None,
		decode=False,
		ignore_existing_file_check=False,
		overwrite=False,
	):
		# write_file (cloud or local) owns naming and dedup; skip Frappe's filesystem rename.
		# With cloud storage on, also skip core's content-hash dedup: it would point this
		# record at an existing *local* file (e.g. a Data Import CSV) and never upload it.
		config = frappe.conf.cloud_storage_settings
		if config and not config.get("use_local"):
			ignore_existing_file_check = True
		return super().save_file(
			content=content,
			decode=decode,
			ignore_existing_file_check=ignore_existing_file_check,
			overwrite=True,
		)

	def after_insert(self):
		# Dedup/association/versioning disabled: keep core behaviour (timeline entry).
		# The old content-hash merge here used rename_doc(merge=True), which wiped the
		# surviving file's s3_key on re-upload and corrupted the stored object link.
		return super().after_insert()

	def create_attachment_record(self):
		# Core quote()s file_url (safe="/:") which corrupts our ?key= link; use it as-is.
		if self.is_folder or not (self.attached_to_doctype and self.attached_to_name):
			return
		from frappe.utils.html_utils import escape_html

		icon = ' <i class="fa fa-lock text-warning"></i>' if self.is_private else ""
		label = escape_html(self.file_name or self.file_url)
		self.add_comment_in_reference_doc(
			"Attachment", f"<a href='{self.file_url}' target='_blank'>{label}</a>{icon}"
		)

	def on_trash(self) -> None:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: on_trash
		"""
		if self.is_home_folder or self.is_attachments_folder:
			frappe.throw(_("Cannot delete Home and Attachments folders"))
		if self.is_attached_to_submitted_doc() and not (
			frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()
		):
			frappe.throw(
				_("This file is attached to a submitted document and cannot be deleted"),
				frappe.PermissionError,
			)
		self.validate_empty_folder()
		self.validate_protected_file()
		if self.file_association:
			return
		# like core: only touch the disk once the deletion is committed, so a rollback
		# never leaves a File record pointing at a deleted file
		frappe.db.after_commit.add(self._delete_file_on_disk)
		if not self.is_folder:
			self.add_comment_in_reference_doc("Attachment Removed", self.file_name)

	def is_attached_to_submitted_doc(self) -> bool:
		if self.is_folder or not (self.attached_to_doctype and self.attached_to_name):
			return False
		try:
			return frappe.db.get_value(self.attached_to_doctype, self.attached_to_name, "docstatus") == 1
		except Exception:
			# attached doctype no longer exists / is a single without docstatus
			return False

	def associate_files(
		self, attached_to_doctype: str | None = None, attached_to_name: str | None = None
	) -> None:
		attached_to_doctype = attached_to_doctype or self.attached_to_doctype  # type: ignore
		attached_to_name = attached_to_name or self.attached_to_name  # type: ignore

		if not attached_to_doctype:
			return
		if not self.file_url:  # type: ignore
			client = get_cloud_storage_client()
			path = get_file_path(self, client.folder)
			self.file_url = FILE_URL.format(path=path)
		if not self.content_hash and "/api/method/retrieve" in self.file_url:  # type: ignore
			associated_doc = frappe.get_value("File", {"file_url": self.file_url}, "name")  # type: ignore
		else:
			associated_doc = frappe.get_value(
				"File",
				{"content_hash": self.content_hash, "name": ["!=", self.name], "is_folder": False},  # type: ignore
			)
		if associated_doc and associated_doc != self.name:
			existing_file = frappe.get_doc("File", associated_doc)
			existing_file.attached_to_doctype = attached_to_doctype
			existing_file.attached_to_name = attached_to_name
			existing_file.append(
				"file_association",
				add_child_file_association(attached_to_doctype, attached_to_name),
			)
			existing_file.save()
		else:
			if self.file_association:
				already_linked = any(
					assoc.link_doctype == attached_to_doctype and assoc.link_name == attached_to_name
					for assoc in self.file_association
				)
				if not already_linked:
					self.append(
						"file_association",
						add_child_file_association(attached_to_doctype, attached_to_name),
					)
			else:
				self.append(
					"file_association",
					add_child_file_association(attached_to_doctype, attached_to_name),
				)

	def add_file_version(self, version_id):
		self.append(
			"versions",
			{
				"version": str(version_id),
				"user": frappe.session.user,
				"timestamp": get_datetime(),
			},
		)
		if not self.is_new():
			# File already exists in DB (filename-conflict path in write_file).
			# Frappe's save lifecycle won't persist this file's child tables,
			# so we insert the version record directly.
			frappe.get_doc(
				{
					"doctype": "File Version",
					"parent": self.name,
					"parenttype": "File",
					"parentfield": "versions",
					"version": str(version_id),
					"user": frappe.session.user,
					"timestamp": get_datetime(),
				}
			).insert(ignore_permissions=True)

	def is_attached_to(self, dt: str, dn: str) -> bool:
		return (self.attached_to_doctype == dt and self.attached_to_name == dn) or any(
			row.link_doctype == dt and row.link_name == dn for row in self.file_association
		)

	def remove_file_association(self, dt: str, dn: str) -> None:
		if len(self.file_association) <= 1:
			# last link: delete the File. delete_doc checks delete permission, and the request's
			# own transaction commits (or rolls back) both statements together.
			frappe.db.delete("File Association", {"parent": self.name})
			frappe.delete_doc("File", self.name)
			return
		to_remove = []
		for idx, row in enumerate(self.file_association):
			if row.link_doctype == dt and row.link_name == dn:
				to_remove.append(row)
				if row.link_doctype == self.attached_to_doctype and row.link_name == self.attached_to_name:  # type: ignore
					# calculate the index of the next file association in the list, looping to the start if already at the end
					next_idx = (idx + 1) % len(self.file_association)
					next_file_association = self.file_association[next_idx]
					self.attached_to_doctype = next_file_association.link_doctype
					self.attached_to_name = next_file_association.link_name
		for row in to_remove:
			self.remove(row)
		for idx, association in enumerate(self.file_association, start=1):
			association.idx = idx
		self.save()

	def get_content(self, encodings=None) -> bytes | str:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: get_content

		Same contract as core (incl. `encodings`, which ERPNext/india_compliance pass);
		only objects in cloud storage are read from the bucket instead of from disk.
		Not whitelisted (as in core): the browser preview uses get_preview_content.
		"""
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		if self.get("content") or not (self.file_url or "").startswith("/api/method/retrieve"):
			return super().get_content(encodings=encodings)

		client = get_cloud_storage_client()
		key = self.s3_key or parse_qs(urlparse(self.file_url).query).get("key", [None])[0]
		try:
			self._content = client.get_object(Bucket=client.bucket, Key=key)["Body"].read()
		except ClientError as e:
			if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
				frappe.throw(
					_("File {0} is missing from cloud storage").format(self.file_name),
					frappe.DoesNotExistError,
				)
			raise

		# decode exactly like core does for files on disk
		if encodings is None:
			encodings = FILE_ENCODING_OPTIONS
		if not self._content.startswith(OLE_FILE_SIGNATURE):
			for encoding in encodings:
				try:
					self._content = self._content.decode(encoding)
					break
				except UnicodeDecodeError:
					continue
		return self._content

	@frappe.whitelist()
	def get_preview_content(self) -> bytes:
		"""Raw bytes for the in-browser DOCX preview in file.js (read permission is checked
		by run_doc_method)."""
		return self.get_content(encodings=())

	def get_full_path(self):
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: get_full_path
		"""
		"""Returns file path from given file name"""

		if self.is_cloud_file():
			# Code in core/ERPNext opens files by path (importers, unzip, xlsx readers, ...);
			# give it a read-only local copy of the object.
			return self.get_cached_path()

		file_path = self.file_url or self.file_name

		site_url = get_url()
		if "/files/" in file_path and file_path.startswith(site_url):
			file_path = file_path.split(site_url, 1)[1]

		if "/" not in file_path:
			if self.is_private:
				file_path = f"/private/files/{file_path}"
			else:
				file_path = f"/files/{file_path}"

		if file_path.startswith("/private/files/"):
			file_path = get_files_path(*file_path.split("/private/files/", 1)[1].split("/"), is_private=1)

		elif file_path.startswith("/files/"):
			file_path = get_files_path(*file_path.split("/files/", 1)[1].split("/"))

		elif file_path.startswith(URL_PREFIXES):
			pass

		elif not self.file_url:
			frappe.throw(_("There is some problem with the file url: {0}").format(file_path))

		if not is_safe_path(file_path):
			frappe.throw(_("Cannot access file path {0}").format(file_path))

		if os.path.sep in self.file_name:
			frappe.throw(_("File name cannot have {0}").format(os.path.sep))

		return file_path

	def is_cloud_file(self) -> bool:
		return (self.file_url or "").startswith("/api/method/retrieve")

	def exists_on_disk(self):
		# A cloud object is never "on disk": core's duplicate check must not download it into
		# the cache and then re-point a new local upload at the cloud object.
		if self.is_cloud_file():
			return False
		return super().exists_on_disk()

	def on_rollback(self):
		# Core restores the previous bytes/path on disk. For a cloud file the previous object is
		# untouched in the bucket - writing "original content" to get_full_path() would only put
		# stale bytes into the local cache.
		if self.is_cloud_file() and not self.flags.new_file:
			for flag in ("original_content", "original_path"):
				self.flags.pop(flag, None)
			return
		return super().on_rollback()

	def get_cached_path(self) -> str:
		"""Local copy of this File's object, downloaded on first use.

		Objects are immutable per key (keys embed the content hash), so a cached copy stays
		valid; the size check catches partial/foreign files. Copies live outside the served
		and backed-up `files` folders, are written atomically (concurrent workers never see a
		half-written file) and are pruned by prune_local_cache() when unused.
		"""
		client = get_cloud_storage_client()
		key = self.s3_key or parse_qs(urlparse(self.file_url).query).get("key", [None])[0]
		if not key:
			frappe.throw(_("There is some problem with the file url: {0}").format(self.file_url))

		digest = hashlib.sha256(f"{client.bucket}/{key}".encode()).hexdigest()
		folder = os.path.join(get_local_cache_dir(), digest[:2], digest)
		path = os.path.join(folder, get_safe_file_name(self.file_name or os.path.basename(key)))

		if os.path.isfile(path) and (not self.file_size or os.path.getsize(path) == cint(self.file_size)):
			os.utime(path)  # mark as recently used for prune_local_cache
			return path

		os.makedirs(folder, mode=0o700, exist_ok=True)
		fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part-")
		try:
			with os.fdopen(fd, "wb") as f:
				try:
					body = client.get_object(Bucket=client.bucket, Key=key)["Body"]
				except ClientError as e:
					if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
						frappe.throw(
							_("File {0} is missing from cloud storage").format(self.file_name),
							frappe.DoesNotExistError,
						)
					raise
				shutil.copyfileobj(body, f, 1024 * 1024)
			os.replace(tmp, path)
		except BaseException:
			with contextlib.suppress(FileNotFoundError):
				os.remove(tmp)
			raise
		return path

	@frappe.whitelist()
	def get_pdf_preview(self):
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		ext = self.file_name.rsplit(".", 1)[-1].lower() if "." in self.file_name else "bin"
		content = self.get_content(encodings=())
		if isinstance(content, str):
			content = content.encode()
		return convert_to_pdf_base64(content, ext)


def convert_to_pdf_base64(content: bytes, ext: str) -> str:
	"""Convert a document to PDF with LibreOffice. Each call gets its own temp dir and
	LibreOffice profile (parallel conversions don't collide), a hard timeout (a stuck
	conversion can't hang the worker), and leaves nothing behind."""
	timeout = cint((frappe.conf.cloud_storage_settings or {}).get("preview_timeout")) or 120
	with tempfile.TemporaryDirectory(prefix="cs-preview-") as tmpdir:
		tmpdir_path = Path(tmpdir)
		source = tmpdir_path / f"document.{re.sub(r'[^a-z0-9]', '', ext) or 'bin'}"
		source.write_bytes(content)
		try:
			subprocess.run(
				[
					# GNU timeout kills LibreOffice's whole process group on expiry
					"timeout",
					"-k",
					"10",
					str(timeout),
					"libreoffice",
					f"-env:UserInstallation={(tmpdir_path / 'profile').as_uri()}",
					"--headless",
					"--norestore",
					"--convert-to",
					"pdf",
					"--outdir",
					str(tmpdir_path),
					str(source),
				],
				check=True,
				capture_output=True,
				timeout=timeout + 30,
			)
		except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
			frappe.log_error("Cloud Storage: document preview failed", f"{e}\n{getattr(e, 'stderr', '')}")
			frappe.throw(_("Could not generate a preview for this file."))

		pdf_path = source.with_suffix(".pdf")
		if not pdf_path.is_file():
			frappe.throw(_("Could not generate a preview for this file."))
		return base64.b64encode(pdf_path.read_bytes()).decode("utf-8")


def get_local_cache_dir() -> str:
	# under private/ (never served) but outside private/files (not in file backups)
	return frappe.get_site_path("private", "cloud_storage_cache")


def prune_local_cache() -> None:
	"""Hourly: drop cached copies unused for `cache_ttl_hours` (default 24) and stale partial
	downloads. Safe to run any time: a removed copy is simply downloaded again."""
	ttl = (cint((frappe.conf.cloud_storage_settings or {}).get("cache_ttl_hours")) or 24) * 3600
	root = get_local_cache_dir()
	if not os.path.isdir(root):
		return
	now = time.time()
	for dirpath, dirnames, filenames in os.walk(root, topdown=False):
		for name in filenames:
			path = os.path.join(dirpath, name)
			with contextlib.suppress(FileNotFoundError):
				max_age = 3600 if name.startswith(".part-") else ttl
				if now - os.path.getmtime(path) > max_age:
					os.remove(path)
		if dirpath != root:
			with contextlib.suppress(OSError):
				os.rmdir(dirpath)  # only succeeds when empty


def is_safe_path(path: str) -> bool:
	if path.startswith(URL_PREFIXES):
		return True

	basedir = frappe.get_site_path()
	# ref: https://docs.python.org/3/library/os.path.html#os.path.commonpath
	matchpath = os.path.abspath(path)
	basedir = os.path.abspath(basedir)

	return basedir == os.path.commonpath((basedir, matchpath))


@frappe.whitelist()
def get_sharing_link(docname: str, reset: str | bool | None = None) -> str:
	if isinstance(reset, str):
		reset = json.loads(reset)
	doc = frappe.get_doc("File", docname)
	if doc.is_private:
		frappe.has_permission(
			doctype="File", ptype="share", doc=doc, user=frappe.session.user, throw=True
		)
	else:
		frappe.has_permission(doctype="File", ptype="read", doc=doc, user=frappe.session.user, throw=True)
	if reset and doc.sharing_link:
		# resetting revokes the link everyone else was given: only for those who may edit the file
		frappe.has_permission(doctype="File", ptype="write", doc=doc, user=frappe.session.user, throw=True)
	if reset or not doc.sharing_link:
		doc.db_set("sharing_link", str(uuid.uuid4().int >> 64))
	return f"{get_url()}/api/method/share?key={doc.sharing_link}"


def strip_special_chars(file_name: str) -> str:
	regex = re.compile(r"[^\w\s_.()-]")
	return regex.sub("", file_name)


# One client per (process, settings): building a boto3 client costs ~65ms and a new TLS
# handshake, so reusing it makes every upload/retrieve ~3x faster. Keyed by pid so forked
# workers never share a parent's connection pool; keyed by settings so credential/bucket
# changes in site_config take effect without a restart (and sites on one bench stay apart).
_CLIENT_CACHE: dict = {}
_CLIENT_CACHE_LOCK = threading.Lock()


def get_cloud_storage_client():
	validate_config()

	config: dict = frappe.conf.cloud_storage_settings
	cache_key = (
		os.getpid(),
		*(
			str(config.get(k))
			for k in (
				"access_key",
				"secret",
				"region",
				"endpoint_url",
				"bucket",
				"folder",
				"expiration",
				"max_attempts",
				"connect_timeout",
				"read_timeout",
				"max_pool_connections",
			)
		),
	)
	client = _CLIENT_CACHE.get(cache_key)
	if client:
		return client

	with _CLIENT_CACHE_LOCK:
		client = _CLIENT_CACHE.get(cache_key)
		if client:
			return client

		session = Session(
			aws_access_key_id=config.get("access_key"),
			aws_secret_access_key=config.get("secret"),
			region_name=config.get("region"),
		)
		client_config = Config(
			signature_version="s3v4",
			# boto3>=1.36 defaults to aws-chunked bodies with a CRC32 trailer, which many
			# S3-compatible stores handle poorly. Payload integrity is still enforced: the
			# SigV4 signature covers the body's SHA-256 and upload_file sends Content-MD5.
			request_checksum_calculation="when_required",
			response_checksum_validation="when_required",
			retries={"mode": "standard", "max_attempts": int(config.get("max_attempts", 5))},
			connect_timeout=int(config.get("connect_timeout", 5)),
			read_timeout=int(config.get("read_timeout", 60)),
			max_pool_connections=int(config.get("max_pool_connections", 10)),
			tcp_keepalive=True,
		)
		client = session.client("s3", endpoint_url=config.get("endpoint_url"), config=client_config)
		client.bucket = config.get("bucket")
		client.folder = config.get("folder", None)
		client.expiration = config.get("expiration", 120)
		client.get_presigned_url = types.MethodType(get_presigned_url, client)
		client.get_sharing_url = types.MethodType(get_sharing_url, client)

		if len(_CLIENT_CACHE) >= 32:
			_CLIENT_CACHE.clear()
		_CLIENT_CACHE[cache_key] = client

	return client


def validate_config() -> None:
	config: dict = frappe.conf.cloud_storage_settings

	if not config:
		frappe.throw(
			msg=_("Please setup cloud storage settings in your site configuration file"),
			title=_("Cloud storage not configured"),
		)

	if not config.get("access_key"):
		frappe.throw(
			msg=_("Please setup access_key in your site configuration file"),
			title=_("Cloud storage access key not configured"),
		)

	if not config.get("secret"):
		frappe.throw(
			msg=_("Please setup secret in your site configuration file"),
			title=_("Cloud storage secret not configured"),
		)

	if not config.get("region"):
		frappe.throw(
			msg=_("Please setup region in your site configuration file"),
			title=_("Cloud storage region not configured"),
		)

	if not config.get("bucket"):
		frappe.throw(
			msg=_("Please setup bucket in your site configuration file"),
			title=_("Cloud storage bucket not configured"),
		)


def get_presigned_url(client, key: str):
	files = frappe.get_all("File", filters={"s3_key": key}, fields=["name", "is_private"])
	if not files:
		raise DoesNotExistError(frappe._("The file you are looking for is not available"))

	# Several records can share one object (attachment copies, identical uploads). If any of
	# them is private the object is private: the user must be able to read one of the private
	# records. A public record pointing at the same key never unlocks it.
	private = [f.name for f in files if f.is_private]
	if private and not any(
		frappe.has_permission("File", "read", doc=frappe.get_doc("File", name)) for name in private
	):
		raise frappe.PermissionError(_("You do not have permission to access this file"))

	return client.generate_presigned_url(
		ClientMethod="get_object",
		Params={"Bucket": client.bucket, "Key": key},
		ExpiresIn=client.expiration if private else None,
	)


def get_sharing_url(client, key: str) -> str:
	file = frappe.get_value("File", {"sharing_link": key}, ["name", "s3_key"], as_dict=True)
	if not file:
		raise DoesNotExistError(frappe._("The file you are looking for is not available"))

	return client.generate_presigned_url(
		ClientMethod="get_object", Params={"Bucket": client.bucket, "Key": file.s3_key}
	)


def upload_file(file: File) -> File:
	client = get_cloud_storage_client()
	path = get_file_path(file, client.folder)

	# Upload what save_file() actually processed (decoded, EXIF-stripped, hashed);
	# `content` is the raw input and only a fallback for callers that bypass save_file.
	body = getattr(file, "_content", None) or file.content
	if isinstance(body, str):
		body = body.encode()
	content_type = getattr(file, "content_type", None) or from_buffer(body, mime=True)
	try:
		client.put_object(
			Body=body,
			Bucket=client.bucket,
			Key=path,
			ContentType=content_type,
			# server rejects the PUT if the stored bytes differ from what we sent
			ContentMD5=base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode(),
		)
	except (BotoCoreError, ClientError):
		# Fail loudly: never leave a File record pointing at an object that wasn't uploaded.
		frappe.log_error(f"Cloud Storage upload failed: {path}", frappe.get_traceback())
		frappe.throw(
			_("Could not upload {0} to cloud storage. Please try again.").format(file.file_name),
			title=_("File Upload Failed"),
		)

	# Only point the record at the object once it exists. URL-encode the key in file_url
	# (spaces etc.) so links don't get mangled; s3_key stays raw so retrieve() matches.
	file.db_set({"file_url": FILE_URL.format(path=quote(path)), "s3_key": path})
	return file


def get_file_path(file: File, folder: str | None = None) -> str:
	custom_storage_path_generator = frappe.get_hooks("cloud_storage_path_generator")

	if custom_storage_path_generator and len(custom_storage_path_generator) > 0:
		try:
			generator_fn = frappe.get_attr(custom_storage_path_generator[0])
			return generator_fn(file, folder)
		except Exception as e:
			frappe.log_error(f"Custom path generator failed: {str(e)}", "Cloud Storage Path Error")

	config = frappe.conf.get("cloud_storage_settings", {})
	if config.get("use_legacy_paths", True):
		return _legacy_get_file_path(file, folder)

	# content_hash keys make distinct files collision-proof (set before upload)
	if folder:
		return f"{folder}/{file.content_hash}/{file.file_name}"

	return f"{file.content_hash}/{file.file_name}"


def _legacy_get_file_path(file: File, folder: str | None = None) -> str:
	parent_doctype = file.attached_to_doctype or "No Doctype"

	attached_to_name = ""
	if file.attached_to_name:
		attached_to_name = file.attached_to_name.replace("#", "%23")

	fragments = [
		folder,
		parent_doctype,
		attached_to_name,
		file.content_hash,  # unique per distinct content -> collision-proof key
		file.file_name.replace("#", "%23"),
	]

	valid_fragments: list[str] = list(filter(None, fragments))
	path = "/".join(valid_fragments)
	return path


def get_file_content_hash(content, content_type):
	try:
		stripped_content = strip_exif_data(content, content_type)
		return get_content_hash(stripped_content)
	except UnidentifiedImageError:
		return get_content_hash(content)


# Working files that other apps *write in place* on the filesystem
# (open(get_full_path(), "w")) or locate with frappe.utils.file_manager.get_file_path (which
# only knows local paths). Neither can work against a bucket, so these stay on local disk.
# Read-only consumers of get_full_path() are served from the local cache instead.
# (doctype, attached_to_field); None matches any field.
LOCAL_ONLY_ATTACHMENTS = (
	("Data Import", None),  # Frappe importer
	("Bank Statement Import", None),  # ERPNext rewrites the uploaded file with processed rows
	("Repost Item Valuation", "reposting_data_file"),  # ERPNext repost checkpoint, rewritten in place
	# india_banking_connector: status files are overwritten in place; payment files and
	# connector keys are opened via file_manager.get_file_path.
	("H2H Status Log", None),
	("H2H Payment Log", None),
	("CITI H2H Connector", None),
)


def is_local_only(file: File) -> bool:
	"""Extend per site with cloud_storage_settings.local_only_attachments:
	[["DocType", "fieldname or null"], ...]"""
	extra = (frappe.conf.cloud_storage_settings or {}).get("local_only_attachments") or []
	return any(
		file.attached_to_doctype == doctype and field in (None, file.attached_to_field)
		for doctype, field in (*LOCAL_ONLY_ATTACHMENTS, *(tuple(x) for x in extra))
	)


def write_file(file: File, remove_spaces_in_file_name: bool = True) -> File:
	config = frappe.conf.cloud_storage_settings
	if not config or config.get("use_local", False):
		# Never fail silently: if cloud is unconfigured, log it (and optionally refuse)
		# instead of quietly writing to local disk. `use_local` is an intentional opt-out.
		if not config:
			frappe.log_error(
				"cloud_storage_settings missing - file stored on LOCAL disk instead of object storage",
				"Cloud Storage: local fallback",
			)
			if frappe.conf.get("require_cloud_storage"):
				frappe.throw(_("Cloud storage is not configured; refusing to store file locally."))
		file.save_file_on_filesystem()
		return file

	if is_local_only(file):
		file.save_file_on_filesystem()
		return file

	# Dedup/association/versioning disabled: every upload is an independent file.
	if remove_spaces_in_file_name:
		file.file_name = file.file_name.replace(" ", "_")

	file.file_name = strip_special_chars(file.file_name)
	file.flags.cloud_storage = True
	return upload_file(file)


def delete_file(file: File, **kwargs) -> File:
	# Retention: deleting a File in ERPNext removes the record but KEEPS the object in
	# object storage (never delete from the bucket). Local-only files (Data Import /
	# use_local) are still cleaned from disk.
	is_cloud = bool(file.get("s3_key")) or (file.file_url or "").startswith("/api/method/retrieve")
	if not is_cloud:
		file.delete_file_from_filesystem(only_thumbnail=kwargs.get("only_thumbnail", False))
	return file


@frappe.whitelist()
def validate_file_content(*args, **kwargs):
	# Dedup disabled: never flag files as already existing (no "already exists" prompt).
	return {"filename_exists": False, "content_exists": False, "matched_files": []}


@frappe.whitelist(allow_guest=True)
def retrieve(key: str) -> None:
	if not key:
		frappe.local.response["http_status_code"] = 404
		frappe.local.response["body"] = "Key not found"
		return

	client = get_cloud_storage_client()
	try:
		signed_url = client.get_presigned_url(key)
	except DoesNotExistError:
		frappe.local.response["http_status_code"] = 404
		frappe.local.response["message"] = _("The file you are looking for is not available or deleted from source")
		return

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = signed_url


@frappe.whitelist(allow_guest=True)
def share(key: str) -> None:
	try:
		if not key:
			raise DoesNotExistError
		signed_url = get_cloud_storage_client().get_sharing_url(key)
	except DoesNotExistError:
		frappe.local.response["http_status_code"] = 404
		frappe.local.response["message"] = _("The file you are looking for is not available")
		return

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = signed_url


@frappe.whitelist(methods=["DELETE", "POST"])
def remove_attach():
	"""
	HASH: 354843a7a42249f2bd1a96706a9ae70dedc610ff
	REPO: https://github.com/frappe/frappe
	PATH: frappe/desk/form/utils.py
	METHOD: remove_attach
	"""
	fid = frappe.form_dict.get("fid")
	dt = frappe.form_dict.get("dt")
	dn = frappe.form_dict.get("dn")
	if not all([fid, dt, dn]):
		return
	doc = frappe.get_doc("File", fid)
	if not doc.is_attached_to(dt, dn):
		frappe.throw(_("This file is not attached to {0} {1}").format(_(dt), dn), frappe.PermissionError)
	doc.remove_file_association(dt, dn)


def add_child_file_association(attached_to_doctype, attached_to_name):
	return {
		"link_doctype": attached_to_doctype,
		"link_name": attached_to_name,
		"user": frappe.session.user,
		"timestamp": get_datetime(),
	}
