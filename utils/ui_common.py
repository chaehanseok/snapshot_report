import streamlit as st
import streamlit.components.v1 as components

def inject_base_css_only(large_mode: bool = False):
    font_scale = "1.18" if large_mode else "1.00"
    css = f"""
    <style>
      header {{ display: none !important; }}
      [data-testid="stToolbar"] {{ display:none !important; }}
      [data-testid="stDecoration"] {{ display:none !important; }}
      [data-testid="stStatusWidget"] {{ display:none !important; }}
      [data-testid="stSidebarNav"] {{ display:none !important; }}
      [data-testid="stSidebarNavSeparator"] {{ display:none !important; }}
      iframe[title="Manage app"] {{ display:none !important; }}
      :root {{ --font-scale: {font_scale}; }}
      html, body, [class*="css"] {{
        font-size: calc(16px * var(--font-scale));
      }}
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)

def inject_global_css():
    st.markdown(
        """
        <style>
        /* ===== Streamlit Chrome 숨김(전역) ===== */
        [data-testid="stToolbar"] { display:none !important; }
        [data-testid="stDecoration"] { display:none !important; }
        [data-testid="stStatusWidget"] { display:none !important; }

        /* Streamlit Cloud Creator avatar 숨김 */
        img[data-testid="appCreatorAvatar"] { display:none !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

def cleanup_token_timer_overlay():
    components.html(
        """
        <script>
        (function () {
          const w = window.parent;

          try {
            // 1) interval 제거
            if (w.__tokenTimerInterval) {
              w.clearInterval(w.__tokenTimerInterval);
              w.__tokenTimerInterval = null;
            }

            // 2) ResizeObserver 제거
            if (w.__tokenTimerRO && Array.isArray(w.__tokenTimerRO)) {
              w.__tokenTimerRO.forEach(function (ro) {
                try { ro.disconnect(); } catch(e) {}
              });
              w.__tokenTimerRO = null;
            }

            // 3) MutationObserver 제거
            if (w.__tokenTimerMO) {
              try { w.__tokenTimerMO.disconnect(); } catch(e) {}
              w.__tokenTimerMO = null;
            }

            // 4) DOM box 제거
            const doc = w.document;
            const box = doc.getElementById("token-timer-fixed-in-main");
            if (box) box.remove();
          } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )
