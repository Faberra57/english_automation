from dotenv import load_dotenv

from english_teacher.config import Settings
from english_teacher.dashboard import render_journal


load_dotenv()
render_journal(Settings.from_env())
