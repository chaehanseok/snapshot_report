import boto3
import streamlit as st
from datetime import timedelta


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=st.secrets["R2_ENDPOINT"],
        aws_access_key_id=st.secrets["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def generate_presigned_pdf_url(
    r2_key: str,
    expires_in: int = 300,  # 5분
) -> str:
    s3 = boto3.client(
        "s3",
        endpoint_url=st.secrets["R2_ENDPOINT"],
        aws_access_key_id=st.secrets["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )

    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": st.secrets["R2_BUCKET_NAME"],
            "Key": r2_key,
            # 🔥 이 두 줄이 핵심
            "ResponseContentDisposition": "inline",
            "ResponseContentType": "application/pdf",
        },
        ExpiresIn=expires_in,
    )
