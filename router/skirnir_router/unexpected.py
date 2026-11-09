"""Last line of defence for an exception no handler expected (TM-15).

Without it aiohttp answers 500 and logs "Error handling request" with the full traceback, whose last line is the exception
text. That text can carry data: a database error quoting a value, a backend reply, a prompt fragment. Here the log gets the
exception type, method, path, a reference and the code locations (file, line, function, source line), never the message
or chained exceptions' messages; the client gets a fixed answer with the same reference, so the two can be matched.
"""
import logging
import traceback
import uuid

from aiohttp import web

log = logging.getLogger("skirnir.unexpected")


def answer(ref):
    return web.json_response({"error": {"message": "internal error", "type": "internal_error", "ref": ref}}, status=500,
                             headers={"Cache-Control": "no-store"})


@web.middleware
async def unexpected_error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:  # exception-ok: aiohttp signals deliberate HTTP answers (401, 404, ...) this way
        raise  # exception-ok: passed on unchanged, they are answers, not errors
    except Exception as e:  # noqa: BLE001  # exception-ok: last-resort boundary for what no handler expected; nothing to check beforehand
        ref = uuid.uuid4().hex[:12]
        frames = "".join(traceback.format_list(traceback.extract_tb(e.__traceback__)))
        log.error("unexpected %s in %s %s (ref %s); message withheld, code locations:\n%s",
                  type(e).__name__, request.method, request.path, ref, frames)
        return answer(ref)
