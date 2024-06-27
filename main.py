import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import openai
from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, \
    InputMediaDocument, Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
import os
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
CHANNEL_ID = os.getenv('CHANNEL_ID')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

client = openai.OpenAI(api_key=OPENAI_API_KEY)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

scheduler = AsyncIOScheduler()
scheduler.start()

keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Да", callback_data='confirm_yes')],
    [InlineKeyboardButton(text="Нет", callback_data='confirm_no')]
])


class Form(StatesGroup):
    awaiting_confirmation = State()


def media_group_confirmation_closure():
    sent_media_group_ids = set()

    def is_media_group_processed(media_group_id):
        if media_group_id in sent_media_group_ids:
            return True
        sent_media_group_ids.add(media_group_id)
        return False

    def clear_media_group_ids():
        nonlocal sent_media_group_ids
        sent_media_group_ids.clear()
        logger.info("Cleared media group IDs")

    return is_media_group_processed, clear_media_group_ids


is_media_group_processed, clear_media_group_ids = media_group_confirmation_closure()

scheduler.add_job(clear_media_group_ids, 'interval', days=3)


async def start_handler(message: Message):
    await message.answer('Hello')


async def translate_text(text: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system",
             "content": """
             You are a professional translator. Please translate the following text to Kyrgyz. Ensure
             that the translation is accurate and maintains the original meaning and tone
             """},
            {"role": "user", "content": text}
        ]
    )
    translation = response.choices[0].message.content.strip()
    return translation


async def ask_confirmation(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        caption = message.caption if message.caption else message.text
        logger.info(f"Original Caption: {caption}")

        file_ids = data.get('file_ids', [])
        if message.photo:
            highest_res_photo = max(message.photo, key=lambda photo: photo.file_size)
            file_ids.append(('photo', highest_res_photo.file_id))
        if message.video:
            file_ids.append(('video', message.video.file_id))
        if message.document:
            file_ids.append(('document', message.document.file_id))

        if caption and not data.get('caption'):
            await state.update_data(caption=caption)
        await state.update_data(file_ids=file_ids)

        if message.media_group_id:
            media_group_id = message.media_group_id

            if is_media_group_processed(media_group_id):
                return

            if not data.get('media_group_id'):
                await state.update_data(media_group_id=media_group_id, buttons_sent=False)
            else:
                media_group_id = data.get('media_group_id')
            if message.media_group_id != media_group_id:
                return

        if not data.get('buttons_sent'):
            prompt_message = await message.answer('Точно ли нужно публиковать это сообщение?', reply_markup=keyboard)
            await state.update_data(prompt_message_id=prompt_message.message_id, buttons_sent=True)
            await state.set_state(Form.awaiting_confirmation)

    except Exception as e:
        logger.error(f"Error in ask_confirmation: {e}")


@dp.callback_query(F.data)
async def forward_to_channel(callback: CallbackQuery, state: FSMContext):
    try:
        action = callback.data
        await callback.answer()

        if action == 'confirm_yes':
            data = await state.get_data()
            file_ids = data.get('file_ids', [])
            caption = data.get('caption', '')

            if caption:
                translated_caption = await translate_text(caption)
            else:
                translated_caption = None

            logger.info(f"Translated Caption: {translated_caption}")

            media_group = []
            for i, (media_type, file_id) in enumerate(file_ids):
                if media_type == 'photo':
                    media_group.append(
                        InputMediaPhoto(media=file_id, caption=translated_caption if i == 0 else None)
                    )
                elif media_type == 'video':
                    media_group.append(
                        InputMediaVideo(media=file_id, caption=translated_caption if i == 0 else None)
                    )
                elif media_type == 'document':
                    media_group.append(
                        InputMediaDocument(media=file_id, caption=translated_caption if i == 0 else None)
                    )

            logger.info(f"Media group to send: {media_group}")

            if media_group:
                await bot.send_media_group(chat_id=CHANNEL_ID, media=media_group)
            elif translated_caption:
                await bot.send_message(chat_id=CHANNEL_ID, text=translated_caption)

            await callback.message.answer("Сообщение отправлено.")
        else:
            await callback.message.answer("Сообщение не отправлено.")

        await state.clear()
    except Exception as e:
        logger.error(f"Error in forward_to_channel: {e}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    dp.message.register(start_handler, Command("start"))
    dp.message.register(ask_confirmation)
    dp.callback_query.register(forward_to_channel,
                               lambda callback_query: callback_query.data in ['confirm_yes', 'confirm_no'])

    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
