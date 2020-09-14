import functools
import logging
import os
import traceback

from telethon.events import StopPropagation
from telethon.events.newmessage import NewMessage

IS_DEBUG = os.getenv("DEBUG") == "True"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.DEBUG if IS_DEBUG else logging.INFO,
)
logger = logging.getLogger(__name__)


def code(text: str) -> str:
    return f"<code>{text}</code>"


def log_on_error():
    def wrapper(function):
        @functools.wraps(function)
        async def wrapped(self, *args, **kwargs):
            event: NewMessage.Event
            (event, *_) = args

            try:
                await function(self, *args, **kwargs)
                raise StopPropagation
            except StopPropagation as stop_propogation:
                raise stop_propogation
            except Exception as error:
                logger.exception(f"Error: {error} on event: {event}")

                # To user
                await event.message.respond(
                    "Произошла непредвиденная ошибка.. Попробуем разобраться"
                )

                # To admin
                error_str, traceback_str, event_str = (
                    str(data)[:1000] for data in (error, traceback.format_exc(), event)
                )  # Limit 4096 UTF-8 characters
                await self.bot.send_message(
                    "@zotho",
                    (
                        f"Avatar bot error:\n {code(error_str)}\n\n"
                        f"Traceback:\n {code(traceback_str)}\n"
                        f"Event:\n {code(event_str)}\n"
                    ),
                    parse_mode="html",
                )
                raise

        return wrapped
    return wrapper
