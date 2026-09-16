# Copyright (c) 2024, AgriTheory and contributors
# For license information, please see license.txt

import base64
import json
import os
import re
import subprocess
import types
import uuid
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import urlopen
import tempfile

import frappe
from boto3.exceptions import S3UploadFailedError
from boto3.session import Session
from botocore.config import Config
from botocore.exceptions import ClientError
from frappe import DoesNotExistError, _
from frappe.core.doctype.file.file import File, get_files_path
from frappe.core.doctype.file.utils import decode_file_content, get_content_hash
from frappe.model.rename_doc import rename_doc
from frappe.utils import get_datetime, get_url
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
		# Dedup/association disabled: do not auto-associate on save (this was the
		# source of the infinite-recursion 500 between duplicate-hash rows).
		if self.flags.cloud_storage or self.flags.ignore_file_validate:
			return
		if not self.is_remote_file:
			self.custom_validate()
		else:
			self.validate_file_url()

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
		user_roles = frappe.get_roles(frappe.session.user)
		if (
			frappe.session.user != "Administrator"
			and "System Manager" not in user_roles
			and (frappe.get_value(self.attached_to_doctype, self.attached_to_name, "docstatus") == 1)  # type: ignore
		):
			frappe.throw(
				_("This file is attached to a submitted document and cannot be deleted"),
				frappe.PermissionError,
			)
		if self.is_home_folder or self.is_attachments_folder:
			frappe.throw(_("Cannot delete Home and Attachments folders"))
		if len(self.file_association) > 0:
			return
		self.validate_empty_folder()
		self._delete_file_on_disk()
		# even though the code is unreachable, we're keeping it here for reference
		if not self.is_folder and len(self.file_association) > 0:
			self.add_comment_in_reference_doc("Attachment Removed", _("Removed {0}").format(self.file_name))

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

	def remove_file_association(self, dt: str, dn: str) -> None:
		if len(self.file_association) <= 1:
			frappe.db.delete("File Association", {"parent": self.name})
			frappe.db.commit()
			self.delete()
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

	@frappe.whitelist()
	def get_content(self) -> bytes:
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: get_content
		"""
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		if self.get("content"):
			self._content = self.content
			if self.decode:  # type: ignore
				self._content = decode_file_content(self._content)
				self.decode = False
			# self.content = None # TODO: This needs to happen; make it happen somehow
			return self._content

		if self.file_url:
			self.validate_file_url()

		if self.file_url.startswith("/api/method/retrieve"):
			client = get_cloud_storage_client()
			file_object = client.get_object(Bucket=client.bucket, Key=self.s3_key)
			self._content = file_object.get("Body").read()
		elif self.file_url.startswith("http://") or self.file_url.startswith("https://"):
			self._content = urlopen(self.file_url).read()
		else:
			if not self.is_private:
				file_path = frappe.get_site_path("public", "files", self.file_name)
			else:
				file_path = frappe.get_site_path("private", "files", self.file_name)
			with open(file_path, mode="rb") as f:
				self._content = f.read()
				try:
					# for plain text files
					self._content = self._content.decode()
				except UnicodeDecodeError:
					# for .png, .jpg, etc
					pass
		return self._content

	def get_full_path(self):
		"""
		HASH: bfbebb3d3d9c26eb34ed447112fcd46f1dadff00
		REPO: https://github.com/frappe/frappe
		PATH: frappe/core/doctype/file/file.py
		METHOD: get_full_path
		"""
		"""Returns file path from given file name"""

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

	@frappe.whitelist()
	def get_pdf_preview(self):
		if self.is_folder:
			frappe.throw(_("Cannot get file contents of a Folder"))

		ext = self.file_name.split(".")[-1].lower()

		if self.file_url.startswith("/api/method/retrieve"):
			client = get_cloud_storage_client()
			ppt_s3_key = self.s3_key

			with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as temp_file:
				file_bytes = client.get_object(Bucket=client.bucket, Key=ppt_s3_key)["Body"].read()

				temp_file.write(file_bytes)
				temp_file.flush()

				file_path = Path(temp_file.name)

			return convert_to_pdf_base64(file_path)

		else:
			if not self.is_private:
				file_path = Path(frappe.get_site_path("public", "files", self.file_name))
			else:
				file_path = Path(frappe.get_site_path("private", "files", self.file_name))

			return convert_to_pdf_base64(file_path)


def convert_to_pdf_base64(file_path: Path):
	with tempfile.TemporaryDirectory() as tmpdir:
		tmpdir_path = Path(tmpdir)

		subprocess.run(
			[
				"libreoffice",
				"--headless",
				"--convert-to",
				"pdf",
				"--outdir",
				str(tmpdir_path),
				str(file_path),
			],
			check=True,
		)

		pdf_filename = file_path.with_suffix(".pdf").name
		pdf_path = tmpdir_path / pdf_filename

		with open(pdf_path, "rb") as f:
			pdf_bytes = f.read()
			return base64.b64encode(pdf_bytes).decode("utf-8")


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
	if reset or not doc.sharing_link:
		doc.db_set("sharing_link", str(uuid.uuid4().int >> 64))
	return f"{get_url()}/api/method/share?key={doc.sharing_link}"


def strip_special_chars(file_name: str) -> str:
	regex = re.compile(r"[^\w\s_.()-]")
	return regex.sub("", file_name)


@frappe.whitelist()
def get_cloud_storage_client():
	validate_config()

	config: dict = frappe.conf.cloud_storage_settings
	session = Session(
		aws_access_key_id=config.get("access_key"),
		aws_secret_access_key=config.get("secret"),
		region_name=config.get("region"),
	)
	client = session.client(
		"s3", endpoint_url=config.get("endpoint_url"), config=Config(signature_version="s3v4")
	)
	client.bucket = config.get("bucket")
	client.folder = config.get("folder", None)
	client.expiration = config.get("expiration", 120)
	client.get_presigned_url = types.MethodType(get_presigned_url, client)
	client.get_sharing_url = types.MethodType(get_sharing_url, client)

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
	file = frappe.get_value("File", {"s3_key": key}, ["name", "is_private"], as_dict=True)
	if not file:
		raise DoesNotExistError(frappe._("The file you are looking for is not available"))
	expiration = client.expiration if file.is_private else None

	if file.is_private:
		file_doc = frappe.get_doc("File", file.name)
		frappe.has_permission(
			doctype="File", ptype="read", doc=file_doc, user=frappe.session.user, throw=True
		)

	return client.generate_presigned_url(
		ClientMethod="get_object",
		Params={"Bucket": client.bucket, "Key": key},
		ExpiresIn=expiration,
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
	# URL-encode the key in file_url (spaces etc.) so links don't get mangled;
	# s3_key stays raw so retrieve()'s lookup still matches.
	file.db_set("file_url", FILE_URL.format(path=quote(path)))
	content_type = file.content_type or from_buffer(file.content, mime=True)
	try:
		client.put_object(
			Body=file.content, Bucket=client.bucket, Key=path, ContentType=content_type
		)
	except S3UploadFailedError:
		frappe.throw(_("File Upload Failed. Please try again."))
	except Exception as e:
		# Fail loudly: never leave a File record pointing at an object that wasn't uploaded.
		frappe.log_error("File Upload Error", e)
		raise
	file.db_set("s3_key", path)
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


@frappe.whitelist()
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

	if file.attached_to_doctype == "Data Import":
		file.save_file_on_filesystem()
		return file

	# Dedup/association/versioning disabled: every upload is an independent file.
	if remove_spaces_in_file_name:
		file.file_name = file.file_name.replace(" ", "_")

	file.file_name = strip_special_chars(file.file_name)
	file.flags.cloud_storage = True
	return upload_file(file)


@frappe.whitelist()
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
	if key:
		client = get_cloud_storage_client()
		signed_url = client.get_presigned_url(key)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = signed_url

	frappe.local.response["body"] = "Key not found"


@frappe.whitelist(allow_guest=True)
def share(key: str) -> None:
	if key:
		client = get_cloud_storage_client()
		signed_url = client.get_sharing_url(key)
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = signed_url

	frappe.local.response["body"] = "Key not found"


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
	doc.remove_file_association(dt, dn)


def add_child_file_association(attached_to_doctype, attached_to_name):
	return {
		"link_doctype": attached_to_doctype,
		"link_name": attached_to_name,
		"user": frappe.session.user,
		"timestamp": get_datetime(),
	}
