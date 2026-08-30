from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="English Studio",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

navigation = st.navigation(
    [
        st.Page("dashboard_pages/journal.py", title="Journal", icon="📖", url_path="journal", default=True),
        st.Page("dashboard_pages/cards.py", title="Cartes", icon="🗂️", url_path="cartes"),
        st.Page("dashboard_pages/statistics.py", title="Statistiques", icon="📈", url_path="statistiques"),
    ]
)
navigation.run()
