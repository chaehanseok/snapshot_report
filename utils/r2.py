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
    *,
    r2_key: str,
    expires_sec: int = 600,   # 10분
) -> str:
    """
    R2에 저장된 PDF에 대한 presigned GET URL 생성
    """
    r2 = get_r2_client()
    bucket = st.secrets["R2_BUCKET_NAME"]

    url = r2.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket,
            "Key": r2_key,
        },
        ExpiresIn=expires_sec,
    )
    return url
