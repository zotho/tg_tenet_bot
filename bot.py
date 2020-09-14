import asyncio
import io
import logging
import os
import shlex
import subprocess
from tempfile import SpooledTemporaryFile, NamedTemporaryFile
from typing import Awaitable, BinaryIO, Optional, Dict

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from telethon import TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.tl import types
from telethon.tl.types import User, Message
from telethon.events.newmessage import NewMessage

import common

log_on_error = common.log_on_error
logger = logging.getLogger(__name__)


class AvatarBot:
    def __init__(self, bot: TelegramClient):
        self.filter_mode_cache: Dict[int, int] = dict()

        self.bot = bot

        bot.add_event_handler(
            self.start_handler,
            events.NewMessage(
                pattern="/start",
                incoming=True,
                func=lambda e: e.is_private
            )
        )
        bot.add_event_handler(
            self.filter_handler,
            events.NewMessage(
                pattern=r"/filter_([01])",
                incoming=True,
                func=lambda e: e.is_private
            )
        )
        bot.add_event_handler(
            self.image_handler,
            events.NewMessage(
                incoming=True,
                func=lambda e: e.is_private and e.media
            )
        )
        bot.add_event_handler(
            self.username_handler,
            events.NewMessage(
                incoming=True,
                func=lambda e: e.is_private and e.message and e.message.text and e.message.text.startswith("@")
            )
        )

    @classmethod
    async def create(cls, api_id: int, api_hash: str, token: str) -> "AvatarBot":
        coroutine: Awaitable[TelegramClient] = (
            TelegramClient("Avatar bot", api_id, api_hash).start(bot_token=token)
        )
        bot: TelegramClient = await coroutine
        self: AvatarBot = cls(bot)
        return self

    async def start_bot(self):
        await self.bot.catch_up()
        await self.bot.run_until_disconnected()

    @log_on_error()
    async def filter_handler(self, event: NewMessage.Event):
        user_id: int = event.chat_id
        filter_value: int = int(event.pattern_match.group(1))
        self.filter_mode_cache[user_id] = filter_value
        respond: Message = await event.message.respond(
            f"Фильтер для видео установлен в состояние {filter_value}"
        )
        user: User = await event.get_chat()
        await asyncio.sleep(5)
        await self.bot.delete_messages(user, [event.message, respond])

    @log_on_error()
    async def start_handler(self, event: NewMessage.Event):
        user: User = await event.get_chat()
        async with self.bot.action(user, "typing"):
            logger.info(
                f"/start: id:{user.id} username:{user.username} first_name:{user.first_name}"
            )
            avatar_buffer = io.BytesIO()
            await self.bot.download_profile_photo(user, file=avatar_buffer)

        if avatar_buffer.tell() > 0:
            async with self.bot.action(user, "photo", delay=10):
                await self.reply_photo(event, avatar_buffer)
        else:
            logger.info(f"No avatar for @{user.username} ({user.id})")

        await event.message.respond(
            "Вы можете отправить мне любые изображения, видео или юзернеймы и я их обработаю.."
            "Установите фильтр для видео командами /filter_0 /filter_1"
        )

    @log_on_error()
    async def image_handler(self, event: NewMessage.Event):
        user: User = await event.get_chat()
        async with self.bot.action(user, "typing"):
            logger.info(f"Process image: {user.id} {user.username} {user.first_name}")

            try:
                if event.media.document.mime_type.startswith("audio/"):
                    await event.message.respond("Не умею работать с аудио")
                    return
            except AttributeError:
                pass

            avatar_buffer = io.BytesIO()
            await self.bot.download_media(event.message, file=avatar_buffer)

        if avatar_buffer.tell() > 0:
            async with self.bot.action(user, "photo", delay=10):
                await self.reply_photo(event, avatar_buffer)
        else:
            await event.message.respond("Не могу найти фото для загрузки")

    @log_on_error()
    async def username_handler(self, event: NewMessage.Event):
        user: User = await event.get_chat()
        async with self.bot.action(user, "typing"):
            logger.info(f"Process username: {user.id} {user.username} {user.first_name}")
            target_username: str
            target_username, *_ = event.message.text.split()

            await self.download_video_avatar(event, target_username)

    async def download_video_avatar(self, event: NewMessage.Event, target_user: str):
        user: User = await event.get_chat()
        photos = await self.bot.get_profile_photos(target_user)
        if len(photos) == 0:
            await event.message.respond("Не могу найти аватарку")
            return

        first_photo = photos[0]
        video_sizes = first_photo.video_sizes

        if video_sizes is None:
            avatar_buffer = io.BytesIO()
            await self.bot.download_profile_photo(target_user, file=avatar_buffer)

            if avatar_buffer.tell() > 0:
                async with self.bot.action(user, "photo", delay=10):
                    await self.reply_photo(event, avatar_buffer)
            else:
                logger.info(f"No avatar for @{user.username} ({user.id})")
            return

        video_size = video_sizes[0]

        progress_message: Message = await event.message.respond("Загружаю видео: 0%")

        async def progress_callback(current: int, total: int):
            new_progress = f"Загружаю видео: {round(current / total * 100)}%"
            if progress_message.text != new_progress:
                try:
                    await self.bot.edit_message(user, progress_message, new_progress)
                except MessageNotModifiedError:
                    pass

        video_bytes = await self.bot.download_file(
            types.InputPhotoFileLocation(
                id=first_photo.id,
                access_hash=first_photo.access_hash,
                file_reference=first_photo.file_reference,
                thumb_size=video_size.type,
            ),
            None,
            file_size=video_size.size,
            progress_callback=progress_callback,
        )

        if video_bytes:
            async with self.bot.action(user, "video", delay=60):
                message_text: str = event.message.message
                filter_type = 1 if message_text.rpartition(" ")[-1] == "1" else 0
                logger.error(repr(event.message.message))
                await self.process_video(event, video_bytes, progress_message, filter_type=filter_type)
        else:
            await event.message.respond("Не смог скачать видео")

    async def process_video(
            self,
            event: NewMessage.Event,
            video_buffer: bytes,
            progress_message: Optional[Message] = None,
            filter_type: int = 0,
    ):
        user: User = await event.get_chat()
        actual_progress_message: Message
        if progress_message is None:
            actual_progress_message = await event.message.respond("Обрабатываю видео")
        else:
            actual_progress_message = progress_message
            await self.bot.edit_message(user, actual_progress_message, "Обрабатываю видео")

        tempfile = NamedTemporaryFile(suffix=".mp4")
        tempfile.write(video_buffer)

        outfile = NamedTemporaryFile(suffix=".mp4")

        cached_filter_mode: Optional[int] = self.filter_mode_cache.get(event.chat_id)
        actual_filter_mode: int = cached_filter_mode if cached_filter_mode is not None else filter_type
        if actual_filter_mode == 0:
            command = (
                "ffmpeg -y "
                f"-i {tempfile.name} "
                '-filter_complex "'
                "[0:v]crop=in_w/2:in_h:in_w/2:0,reverse[u];"
                "[0:v][u]overlay=w:eof_action=pass"
                f'" {outfile.name}'
            )
        elif actual_filter_mode == 1:
            command = (
                "ffmpeg -y "
                f"-i {tempfile.name} "
                '-filter_complex "'
                "[0:v]crop=in_w/2:in_h:in_w/2:0[r];"
                "[0:v]crop=in_w/2:in_h:0:0,reverse[l];"
                "[0:v][l]overlay=w:eof_action=pass[wl];"
                "[wl][r]overlay=eof_action=pass"
                f'" {outfile.name}'
            )
        else:
            raise NotImplemented

        subprocess.check_call(shlex.split(command))

        async def progress_callback(current: int, total: int):
            new_progress = f"Отправляю видео: {round(current / total * 100)}%"
            if actual_progress_message.text != new_progress:
                try:
                    await self.bot.edit_message(user, actual_progress_message, new_progress)
                except MessageNotModifiedError:
                    pass

        await self.bot.send_file(
            user,
            outfile.name,
            progress_callback=progress_callback,
        )
        await self.bot.delete_messages(user, actual_progress_message)

    async def reply_photo(self, event: NewMessage.Event, avatar_file: BinaryIO):
        try:
            image: Image.Image = Image.open(avatar_file).convert("RGBA")
        except UnidentifiedImageError as image_error:
            logger.warning(f"Could'nt open image. Error: {image_error}. Event: {event}")

            if event.message and event.message.sticker:
                await event.message.respond("Пока что я не умею обрабатывать анимированные стикеры")
                return

            avatar_file.seek(0)
            message_text: str = event.message.message
            logger.error(repr(message_text))
            filter_type = 1 if message_text.rpartition(" ")[-1] == "1" else 0
            await self.process_video(event, avatar_file.read(), filter_type=filter_type)
            return

        try:
            image.verify()
        except Exception as image_error:
            logger.warning(f"Could'nt process image. Error: {image_error}. Event: {event}")
            await event.message.respond("Не могу найти фото для загрузки")
            return

        width, height = size = image.size
        if max(size) > 4000:
            await event.message.respond(f"Слишком большое изображение: {width}:{height}")
            return
        elif min(size) < 10:
            await event.message.respond(f"Слишком маленькое изображение: {width}:{height}")
            return

        mask_im = Image.new("L", size, 0)
        draw: ImageDraw = ImageDraw.Draw(mask_im)
        draw.rectangle((width/2, 0, width, height), fill=255)

        image.paste(ImageOps.flip(image), mask=mask_im)

        image = image.convert("RGBA")
        result_buffer = io.BytesIO()
        image.save(result_buffer, format="PNG")
        result_buffer.seek(0)

        await self.bot.send_file(await event.get_chat(), result_buffer)


async def main():
    api_id: int = int(os.environ["API_ID"])
    api_hash: str = os.environ["API_HASH"]
    token: str = os.environ["TG_TOKEN"]
    bot = await AvatarBot.create(api_id, api_hash, token)
    await bot.start_bot()


if __name__ == "__main__":
    asyncio.run(main())
