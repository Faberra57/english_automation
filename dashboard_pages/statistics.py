from dotenv import load_dotenv

from english_teacher.config import Settings
from english_teacher.dashboard import render_statistics


load_dotenv()
render_statistics(Settings.from_env())
