import pypandoc

content = """# Coverage Snapshot Report

**Coverage Snapshot Report**는  
고객의 연령·성별 기준으로 보장 환경을 요약하여 보여주는  
**사전 니즈 환기(Pre-Analysis)용 리포트 생성 애플리케이션**입니다.

이 리포트는 보험 상품 추천이나 보장 판단을 목적하지 않으며,  
**종합 보장분석(Coverage Analysis) 이전 단계에서  
고객이 스스로 보장 점검의 필요성을 인식하도록 돕는 안내 자료**입니다.

---

## 🎯 Purpose

- 보험 가입 여부가 아닌 **보장 ‘충분성’ 점검의 필요성 환기**
- 연령·성별 기준 **통계 기반 보장 환경 요약**
- 상세 보장분석 리포트로의 **자연스러운 연결**

> 본 앱은 **영업 보조 도구**가 아닌  
> **보장 점검을 위한 사전 안내(Preview) 도구**입니다.

---

## 🧩 What This App Does

1. M.POST 게이트웨이 URL을 통해 설계사 인증  
2. 고객 성명 / 성별 / 연령대 입력  
3. 연령·성별 기준 **표준 콘텐츠 세트 자동 적용**  
4. 통계 기반 보장 환경 요약 미리보기  
5. 확정 후 **PDF 리포트 자동 생성**  
6. 모바일 전달 또는 출력 활용  

---

## 📄 Report Characteristics

- **Report Name**: Coverage Snapshot Report  
- **Format**: PDF (A4 / Mobile Friendly)

### Content
- 연령·성별 기준 보장 환경 요약  
- 주요 점검 질문 (Gap Awareness)  
- 보장 구조 개요 (진단비 / 치료비 / 생활·소득)

### Excludes
- 보험 상품 추천  
- 보장 금액 산출  
- 지급 가능성 판단  

---

## 🛡️ Compliance & Safety

- 본 리포트는 **통계 기반 참고 자료**로만 제공됩니다.  
- 개인별 보장 수준에 대한 **판단·단정 표현을 사용하지 않습니다.**  

> “본 자료는 동일 연령·성별 집단의 통계 기반 참고 자료이며,  
> 개인별 보장 수준은 상이할 수 있습니다.  
> 본 자료는 법적 효력을 갖지 않습니다.”

---

## 🏗️ Project Structure

coverage-snapshot-report/  
├── app.py  
├── requirements.txt  
├── templates/  
│   ├── pamphlet_v1.html  
│   ├── style.css  
│   └── assets/  
├── content/  
│   └── v1/  
│       ├── segments.json  
│       └── stats_phrases.json  
└── README.md  

---

## ⚙️ Tech Stack

- **Streamlit** – Web Application Framework  
- **Jinja2** – HTML Template Rendering  
- **HTML / CSS** – PDF Layout  
- **WeasyPrint / ReportLab** – PDF Generation  
- **HMAC Token Validation** – Secure Gateway Access  

---

## 🔐 Security

- 설계사 정보는 **서명된 토큰(HMAC)** 으로 전달됩니다.  
- 토큰에는 만료 시간(`exp`)이 포함됩니다.  
- 고객 개인정보는 최소한으로 입력받습니다.  

---

## 🚀 Deployment

- **Platform**: Streamlit Cloud  
- **Source Control**: GitHub  

### Required Secrets

GATEWAY_SECRET = "your-secure-random-string"

---

## 📌 Usage Policy

- 본 애플리케이션은 **미래에셋금융서비스 설계사 전용 내부 도구**입니다.  
- 외부 배포 또는 무단 사용을 금합니다.  
- 본 리포트는 **보장분석 리포트 제공을 위한 사전 안내 자료**로만 활용해야 합니다.  

---

## 📎 Disclaimer

Coverage Snapshot Report is provided for informational purposes only  
and does not constitute insurance advice, recommendation, or analysis.  

Final coverage decisions should be made through a full Coverage Analysis Report.

---

## ✉️ Contact

For internal inquiries, improvements, or maintenance requests,  
please contact the project owner or system administrator.
"""

output_path = "/mnt/data/README.md"
pypandoc.convert_text(content, 'md', format='md', outputfile=output_path, extra_args=['--standalone'])

output_path
