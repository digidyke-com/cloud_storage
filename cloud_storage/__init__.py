# Copyright (c) 2024, AgriTheory and contributors
# For license information, please see license.txt

__version__ = "16.2.0"

# NOTE: the previous get_attachments monkey-patch (listing via the File Association
# table) was removed along with the association feature. Frappe's native
# get_attachments — which lists by File.attached_to_doctype/name — is used instead.
