import streamlit as st
import boto3


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=st.secrets["R2_ENDPOINT"],
        aws_access_key_id=st.secrets["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=st.secrets["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def generate_presigned_pdf_url(r2_key: str, expires: int = 300) -> str:
    """
    R2 Private Object에 대한 임시 접근 URL 생성
    """
    r2 = get_r2_client()
    return r2.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": st.secrets["R2_BUCKET_NAME"],
            "Key": r2_key,
        },
        ExpiresIn=expires,
    )
