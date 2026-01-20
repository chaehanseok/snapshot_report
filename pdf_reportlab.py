from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from io import BytesIO
from datetime import datetime

def generate_snapshot_pdf(
    customer_name: str,
    age_band: str,
    gender: str,
    advisor_name: str,
    advisor_phone: str,
):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Title
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(width / 2, height - 40 * mm, "Coverage Snapshot Report")

    # Subtitle
    c.setFont("Helvetica", 11)
    c.drawCentredString(
        width / 2,
        height - 50 * mm,
        "Age & Gender-Based Coverage Overview"
    )

    # Customer Info
    c.setFont("Helvetica", 10)
    c.drawString(30 * mm, height - 70 * mm, f"Customer: {customer_name}")
    c.drawString(30 * mm, height - 78 * mm, f"Profile: {age_band} · {gender}")

    # Divider
    c.line(30 * mm, height - 85 * mm, width - 30 * mm, height - 85 * mm)

    # Body (example text)
    c.setFont("Helvetica", 10)
    text = c.beginText(30 * mm, height - 100 * mm)
    text.textLine("This report provides a statistical overview of coverage considerations")
    text.textLine("based on age and gender group data.")
    text.textLine("")
    text.textLine("It is intended as a preview before a full Coverage Analysis.")
    c.drawText(text)

    # Footer
    c.setFont("Helvetica", 8)
    c.drawString(
        30 * mm,
        20 * mm,
        f"Prepared by {advisor_name} | {advisor_phone}"
    )
    c.drawRightString(
        width - 30 * mm,
        20 * mm,
        datetime.now().strftime("%Y-%m-%d")
    )

    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer
