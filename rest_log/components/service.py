# Copyright 2020 Camptocamp SA (http://www.camptocamp.com)
# @author Guewen Baconnier <guewen.baconnier@camptocamp.com>
# @author Simone Orsi <simahawk@gmail.com>
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html).

import json
import traceback

from werkzeug.urls import url_encode, url_join

from odoo import registry
from odoo.http import Response, request

from odoo.addons.base_rest.http import JSONEncoder
from odoo.addons.component.core import AbstractComponent

from ..exceptions import EXCEPTION_MAP, RESTServiceDispatchException


def json_dump(data):
    """Encode data to JSON as we like."""
    return json.dumps(data, cls=JSONEncoder, indent=4, sort_keys=True)


class BaseRESTService(AbstractComponent):
    _inherit = "base.rest.service"
    # can be overridden to enable logging of requests to DB
    _log_calls_in_db = False

    def dispatch(self, method_name, *args, params=None):
        if not self._db_logging_active(method_name):
            return super().dispatch(method_name, *args, params=params)
        return self._dispatch_with_db_logging(method_name, *args, params=params)

    def _dispatch_with_db_logging(self, method_name, *args, params=None):
        # TODO: consider refactoring thi using a savepoint as described here
        # https://github.com/OCA/rest-framework/pull/106#pullrequestreview-582099258
        try:
            result = super().dispatch(method_name, *args, params=params)
        except Exception as orig_exception:
            exc = self._get_dispatch_with_db_logging_exception(
                method_name, orig_exception, *args, params=params
            )
            return self._dispatch_exception(
                method_name, exc, orig_exception, *args, params=params
            )
        log_entry = self._log_call_in_db(
            self.env, request, method_name, *args, params=params, result=result
        )
        if log_entry and isinstance(result, dict):
            log_entry_url = self._get_log_entry_url(log_entry)
            result["log_entry_url"] = log_entry_url
        return result

    def _get_dispatch_with_db_logging_exception(
        self, method_name, orig_exception, *args, params=None
    ):
        # Hook method: to be overridden to allow retrieving custom exceptions
        exc_map = self._get_dispatch_with_db_logging_exception_map(
            method_name, *args, params=params
        )
        return exc_map.get(type(orig_exception)) or RESTServiceDispatchException

    def _get_dispatch_with_db_logging_exception_map(
        self, method_name, *args, params=None
    ):
        # Hook method: to be overridden to allow custom mappings
        return EXCEPTION_MAP

    def _dispatch_exception(
        self, method_name, exception_klass, orig_exception, *args, params=None
    ):
        tb = traceback.format_exc()
        # TODO: how to test this? Cannot rollback nor use another cursor
        self.env.cr.rollback()
        with registry(self.env.cr.dbname).cursor() as cr:
            env = self.env(cr=cr)
            log_entry = self._log_call_in_db(
                env,
                request,
                method_name,
                *args,
                params=params,
                traceback=tb,
                orig_exception=orig_exception,
            )
            log_entry_url = self._get_log_entry_url(log_entry)
        # UserError and alike have `name` attribute to store the msg
        exc_msg = self._get_exception_message(orig_exception)
        json_info = {"log_entry_url": log_entry_url}
        # Retrieve REST JSON info from original exception (if existing)
        json_info.update(self._get_rest_json_info(orig_exception))
        exc = exception_klass(exc_msg, **json_info)
        raise exc from orig_exception

    def _get_exception_message(self, exception):
        return getattr(exception, "name", str(exception))

    def _get_rest_json_info(self, exception):
        return getattr(exception, "rest_json_info", {})

    def _get_log_entry_url(self, entry):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        url_params = {
            "action": self.env.ref("rest_log.action_rest_log").id,
            "view_type": "form",
            "model": entry._name,
            "id": entry.id,
        }
        url = "/web?#%s" % url_encode(url_params)
        return url_join(base_url, url)

    @property
    def _log_call_header_strip(self):
        return ("Cookie", "Api-Key")

    def _log_call_in_db_values(self, _request, *args, params=None, **kw):
        httprequest = _request.httprequest
        headers = self._log_call_sanitize_headers(dict(httprequest.headers or []))
        params = dict(params or {})
        if args:
            params.update(args=args)

        params = self._log_call_sanitize_params(params)

        result = kw.get("result")
        # NB: ``result`` might be an object of class ``odoo.http.Response``,
        # for example when you try to download a file. In this case, we need to
        # handle it properly, without the assumption that ``result`` is a dict.
        if isinstance(result, Response):
            status_code = result.status_code
            result = {
                "status": status_code,
                "headers": self._log_call_sanitize_headers(dict(result.headers or [])),
            }
            state = "success" if status_code in range(200, 300) else "failed"
        else:
            state = "success" if result else "failed"
        error = kw.get("traceback")
        orig_exception = kw.get("orig_exception")
        exception_name = None
        exception_message = None
        if orig_exception:
            exception_name = orig_exception.__class__.__name__
            if hasattr(orig_exception, "__module__"):
                exception_name = orig_exception.__module__ + "." + exception_name
            exception_message = self._get_exception_message(orig_exception)
        return {
            "collection": self._collection,
            "request_url": httprequest.url,
            "request_method": httprequest.method,
            "params": json_dump(params),
            "headers": json_dump(headers),
            "result": json_dump(result),
            "error": error,
            "exception_name": exception_name,
            "exception_message": exception_message,
            "state": state,
        }

    def _log_call_in_db(self, env, _request, method_name, *args, params=None, **kw):
        values = self._log_call_in_db_values(_request, *args, params=params, **kw)
        enabled_states = self._get_matching_active_conf(method_name)
        if not values or enabled_states and values["state"] not in enabled_states:
            return
        return env["rest.log"].sudo().create(values)

    def _log_call_sanitize_params(self, params: dict) -> dict:
        if "password" in params:
            params["password"] = "<redacted>"
        return params

    def _log_call_sanitize_headers(self, headers: dict) -> dict:
        for header_key in self._log_call_header_strip:
            if header_key in headers:
                headers[header_key] = "<redacted>"
        return headers

    def _db_logging_active(self, method_name):
        enabled = self._log_calls_in_db
        if not enabled:
            enabled = bool(self._get_matching_active_conf(method_name))
        return request and enabled and self.env["rest.log"].logging_active()

    def _get_matching_active_conf(self, method_name):
        return self.env["rest.log"]._get_matching_active_conf(
            self._collection, self._usage, method_name
        )
