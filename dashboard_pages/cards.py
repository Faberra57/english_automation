from dotenv import load_dotenv

from english_teacher.config import Settings
from english_teacher.dashboard import render_cards


load_dotenv()
render_cards(Settings.from_env())
