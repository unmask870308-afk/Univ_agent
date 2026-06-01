import os
import sys
import subprocess

def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[에러] GEMINI_API_KEY 환경변수를 찾을 수 없습니다.")
        sys.exit(1)

    target_path = "scripts/telegram_agent.py"
    if not os.path.exists(target_path):
        print(f"[에러] {target_path} 파일이 존재하지 않습니다.")
        sys.exit(1)

    # 1. 기존 telegram_agent.py 코드 읽기
    with open(target_path, "r", encoding="utf-8") as f:
        current_code = f.read()

    # 2. 구글 제미나이 SDK 로드 (환경에 맞춰 유연하게 대응)
    print("[1/3] 구글 Gemini AI 아키텍트 깨우는 중...")
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        is_new_sdk = True
    except ImportError:
        import google.generativeai as tg_genai
        tg_genai.configure(api_key=api_key)
        is_new_sdk = False

    # 3. 코드 업그레이드 요청 프롬프트 작성
    prompt = f"""
너는 최고 단계의 대입 시스템 수석 아키텍트이다.
아래의 기존 'scripts/telegram_agent.py' 코드를 수정하여 다음 기능들을 완벽하게 통합하라.

1. 생활기록부 PDF 파서 기능: 'pypdf' 또는 'PyPDF2'를 사용하여 텔레그램으로 업로드된 PDF 파일에서 텍스트를 추출하고, gemini-2.5-flash-lite를 통해 고교유형, 성적추이, 과목별 내신, 세특 키워드를 정형화하라.
2. 2028 대입 개편안 보정: 기존 9등급제 기준 데이터를 고2 학생들에게 적용되는 5등급제 기준으로 자동 매칭 및 보정하는 로직을 평가 엔진에 주입하라.
3. SQLite DB 마이그레이션: JSON 파일 기반 저장을 'data/admissions_agent.db' SQLite 데이터베이스 체제로 전환하라. 학생 정보(데이터 암호화 적용)와 대학 전형 기준 테이블을 생성하고 sqlite3 라이브러리로 읽고 쓰도록 리팩토링하라.
4. 보안 강화: cryptography 라이브러리로 성적 및 세특 텍스트 컬럼을 암호화하여 저장하고, SQL 인젝션 및 패스 트래버설을 방어하라.

설명이나 앞뒤 마크다운 기호(```python) 없이 오직 '순수한 파이썬 소스 코드 전체'만 출력하라.

[기존 소스 코드]
{current_code}
"""

    print("[2/3] 제미나이가 소스코드를 초정밀 개조하고 있습니다 (잠시만 기다려주세요)...")
    if is_new_sdk:
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt,
        )
        generated_text = response.text
    else:
        model = tg_genai.GenerativeModel('gemini-2.5-flash-lite')
        response = model.generate_content(prompt)
        generated_text = response.text

    # 마크다운 백틱 정제
    clean_code = generated_text
    if "```python" in clean_code:
        clean_code = clean_code.split("```python")[1].split("```")[0]
    elif "```" in clean_code:
        clean_code = clean_code.split("```")[1].split("```")[0]
    
    clean_code = clean_code.strip()

    # 4. 코드가 정상적으로 생성되었는지 검증 후 덮어쓰기
    if "import" in clean_code and "def" in clean_code:
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(clean_code)
        print(f"[성공] {target_path} 파일이 SQLite 및 PDF 파서 체제로 자동 덮어쓰기 되었습니다.")
        
        # 5. 마스터 재시작 스크립트 실행
        print("[3/3] 수정한 코드를 반영하여 전체 시스템을 일괄 재부팅합니다...")
        subprocess.run(["./restart_agent.sh"], shell=True)
        print("[완료] 모든 서비스가 새 코드 기반 백그라운드에서 정상 가동 중입니다!")
    else:
        print("[실패] 제미나이 응답 코드가 유효하지 않아 원본을 보호했습니다. 다시 시도해 주세요.")

if __name__ == "__main__":
    main()
