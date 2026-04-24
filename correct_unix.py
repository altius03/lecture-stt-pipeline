#!/usr/bin/env python3
"""
Unix 과목 3월 전사문 ASR 교정 스크립트
"""

import re
import json
import os

BASE_SRC = "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/02_transcripts"
BASE_DST = "/Users/geonha/Library/Mobile Documents/com~apple~CloudDocs/lecture_recordings/03_correction"

STEMS = [
    "260304Unix_1", "260304Unix_2",
    "260310Unix_1", "260310Unix_2",
    "260317Unix_1", "260317Unix_2",
    "260319Unix_1", "260319Unix_2",
    "260324Unix_1", "260324Unix_2",
    "260325Unix_1", "260325Unix_2",
    "260331Unix_1", "260331Unix_2",
]

# (pattern, replacement) — 순서 중요: 긴/구체적 패턴 먼저
CORRECTIONS = [
    # 공통: 전자출결
    (r'전자시리얼', '전자출결'),
    (r'전자실교', '전자출결'),
    # 공통: 컴퓨터
    (r'컴퍼런스', '컴퓨터'),
    (r'컴퍼넛', '컴퓨터'),
    (r'컴퍼드', '컴퓨터'),
    (r'컴푼터', '컴퓨터'),
    (r'컴퓨드', '컴퓨터'),
    # 운영체제 (긴 변형 먼저)
    (r'운영체해', '운영체제'),
    (r'운영체의', '운영체제'),
    (r'운영시제', '운영체제'),
    (r'운영 시제', '운영체제'),
    (r'운영시세', '운영체제'),
    (r'운영 시세', '운영체제'),
    (r'운영시절', '운영체제'),
    (r'운영 시절', '운영체제'),
    (r'운영시대', '운영체제'),
    (r'운영 시대', '운영체제'),
    (r'운영 프레임이\b', '운영체제가'),
    (r'운영프레임이\b', '운영체제가'),
    (r'운영 프레임', '운영체제'),
    (r'운영프레임', '운영체제'),
    # 커널 (긴 것 먼저)
    (r'터네일링트', '커널'),
    (r'터네일', '커널'),
    (r'터덜', '커널'),
    (r'터럴', '커널'),
    (r'터너', '커널'),
    (r'터널', '커널'),
    (r'털널', '커널'),
    (r'털너', '커널'),
    (r'코너', '커널'),
    (r'코널', '커널'),
    # 유닉스
    (r'릴릭스', '유닉스'),
    (r'릴리스', '유닉스'),
    (r'릴렉스', '유닉스'),
    (r'릴링스', '유닉스'),
    (r'릴리', '유닉스'),
    (r'유닉시', '유닉스'),
    (r'유닉쓰', '유닉스'),
    (r'유닉슨', '유닉스'),
    (r'윙스', '유닉스'),
    (r'뉴익스', '유닉스'),
    (r'유릭스', '유닉스'),
    (r'유리스', '유닉스'),
    # 리눅스
    (r'이룩스', '리눅스'),
    (r'이룩시', '리눅스'),
    (r'릴스', '리눅스'),
    (r'리뉴스', '리눅스'),
    (r'리눅시', '리눅스'),
    (r'리뉴즈', '리눅스'),
    (r'리뉴츠', '리눅스'),
    (r'리룩스', '리눅스'),
    (r'리뉴수', '리눅스'),
    (r'릴수', '리눅스'),
    # 시스템 호출 (긴 것 먼저)
    (r'시스템모추', '시스템 호출'),
    (r'시스템모출', '시스템 호출'),
    (r'시스템모축', '시스템 호출'),
    (r'시스텝노출', '시스템 호출'),
    (r'시스템 노출', '시스템 호출'),
    (r'시스텔 노출', '시스템 호출'),
    (r'시스템붙이', '시스템 호출'),
    (r'시스텝노충', '시스템 호출'),
    (r'시스템보충', '시스템 호출'),
    (r'시스템모충', '시스템 호출'),
    # 시스템
    (r'시스텔', '시스템'),
    (r'서스텔리아어', '시스템'),
    (r'서스텔스', '시스템'),
    # 디바이스 드라이버
    (r'디바이스라이브', '디바이스 드라이버'),
    (r'디바이스라이버', '디바이스 드라이버'),
    (r'디바이스 라이버', '디바이스 드라이버'),
    (r'디스크 드라이브', '디바이스 드라이버'),
    (r'디바이드 드라이버', '디바이스 드라이버'),
    (r'디바이실', '디바이스'),
    # 인터페이스
    (r'인터베이스', '인터페이스'),
    (r'인터페이지', '인터페이스'),
    # GUI
    (r'\bGLI\b', 'GUI'),
    # 모노리식 커널
    (r'모노리스 커널', '모노리식 커널'),
    (r'모노리트 커널', '모노리식 커널'),
    (r'모노이드 커널', '모노리식 커널'),
    (r'모노루티컨', '모노리식 커널'),
    (r'모노르티컨', '모노리식 커널'),
    (r'모노로티컨', '모노리식 커널'),
    (r'모노리스 터널', '모노리식 커널'),
    (r'모노리트 터널', '모노리식 커널'),
    (r'모노이드 터널', '모노리식 커널'),
    # 마이크로 커널
    (r'마이크로 터네일', '마이크로 커널'),
    (r'마이코 터너', '마이크로 커널'),
    (r'마이크라커너', '마이크로 커널'),
    (r'마이크로폰', '마이크로 커널'),
    (r'마이크로커너', '마이크로 커널'),
    # 하이브리드 커널
    (r'하이브리드 터네일링트', '하이브리드 커널'),
    (r'하이브리드 커너', '하이브리드 커널'),
    # 특권 명령 / 비특권 명령
    (r'특권면령', '특권 명령'),
    (r'특궐명령', '특권 명령'),
    (r'특허명령', '특권 명령'),
    (r'특허 명령', '특권 명령'),
    (r'비트컵', '비특권'),
    (r'비트컴', '비특권'),
    (r'피트컴', '비특권'),
    (r'비특검', '비특권'),
    # 커널 모드
    (r'코널모드', '커널 모드'),
    # BSD/System V/Solaris/macOS
    (r'\bBST\b', 'BSD'),
    (r'바꾸체', 'BSD'),
    (r'서브 S\b', 'System V'),
    (r'시스템 5\b', 'System V'),
    (r'쏘라이스', 'Solaris'),
    (r'매그 웨스트', 'macOS'),
    # C 언어
    (r'시어너', 'C 언어'),
    (r'시현어', 'C 언어'),
    # 어셈블리
    (r'어션블리', '어셈블리'),
    (r'어셈블릭', '어셈블리'),
    (r'어셈브리도', '어셈블리도'),
    (r'어셈브리드', '어셈블리'),
    (r'어셈블이', '어셈블리'),
    # 이식성
    (r'포토빌', '이식성'),
    # 컴파일
    (r'컴팔', '컴파일'),
    # GNU
    (r'\bGND\b', 'GNU'),
    (r'\bGMD\b', 'GNU'),
    (r'그놋', 'GNU'),
    (r'근후', 'GNU'),
    (r'그누', 'GNU'),
    (r'근우', 'GNU'),
    # GPL
    (r'\bGP\b', 'GPL'),
    # 자유 소프트웨어 재단
    (r'자율수호케어 재단', '자유 소프트웨어 재단'),
    (r'자율 소프트웨어', '자유 소프트웨어'),
    (r'프리소프트 웨어 재담', '자유 소프트웨어 재단'),
    (r'자율소프트웨러', '자유 소프트웨어'),
    (r'자율수급', '자유 소프트웨어'),
    # 리눅스 재단
    (r'리뉴스 재단', '리눅스 재단'),
    (r'리뉴수재단', '리눅스 재단'),
    (r'위릴수 재단', '리눅스 재단'),
    (r'리룩스 재단', '리눅스 재단'),
    # 배포판
    (r'분양시절', '배포판'),
    (r'분역시기', '배포판'),
    (r'푼양시절', '배포판'),
    # 프로그램
    (r'코렘', '프로그램'),
    (r'프로랄드', '프로그램'),
    (r'프로랄', '프로그램'),
    # 이론
    (r'이혼', '이론'),
    # 다중 사용자
    (r'다중사형시스템', '다중 사용자 시스템'),
    (r'멀티유선', '다중 사용자'),
    (r'다중 사업자', '다중 사용자'),
    # 가상화
    (r'\bVNL\b', 'VMware'),
    (r'\bVML\b', 'VMware'),
    (r'버츄얼박스', 'VirtualBox'),
    (r'가상 먴신', '가상 머신'),
    (r'가상 모시', '가상 머신'),
    (r'가상머니', '가상 머신'),
    # 호스트 (문맥상 "포스트" → "호스트" 는 강의에서 자주 나오나, 일부 "포스트"는 실제 발화일 수 있어 보수적으로 처리)
    # 데스크톱
    (r'사리스크', '데스크톱'),
    # 디렉토리
    (r'디렛토리', '디렉토리'),
    (r'디렙터리', '디렉토리'),
    # 라이브러리
    (r'라이프라리', '라이브러리'),
    (r'라이브라리', '라이브러리'),
    (r'라이버리', '라이브러리'),
    (r'라이벌이', '라이브러리'),
    (r'라이브리드', '라이브러리'),
    (r'라이비드', '라이브러리'),
    # 실행파일
    (r'시행파일', '실행파일'),
    (r'시행판', '실행파일'),
    (r'생파이어', '실행파일'),
    # 셸
    (r'쇼피워드', '셸'),
    # 수시고사
    (r'수치고사', '수시고사'),
    (r'수시보사', '수시고사'),
    (r'수시국사', '수시고사'),
    (r'수집고사', '수시고사'),
    # 상대평가
    (r'상대폭가', '상대평가'),
    (r'상대폐가', '상대평가'),
    # E-클래스
    (r'이 클래스', 'E-클래스'),
    # ChatGPT 등은 이 과목 맥락에서 낮은 빈도 → 필요시 처리
    # 파이썬
    (r'파이써', '파이썬'),
    (r'파이쎤', '파이썬'),
    (r'파이쎈', '파이썬'),
    # 알고리즘
    (r'알고르즘', '알고리즘'),
    # 프로세스 추가 변형
    (r'프로세서(?=\s*(?:관리|생성|종료|스케줄|실행|상태|동작|전환|목록|생명))', '프로세스'),
    # VS Code
    (r'BS코드', 'VS Code'),
    (r'bscode', 'VS Code'),
]

POST_CORRECTIONS = [
    # 교정 후 조사 불일치 해결
    (r'운영체제이\s', '운영체제가 '),
    (r'운영체제이\b', '운영체제가'),
]

def correct_line(text):
    result = text
    for pat, rep in CORRECTIONS:
        result = re.sub(pat, rep, result)
    for pat, rep in POST_CORRECTIONS:
        result = re.sub(pat, rep, result)
    return result

def process_stem(stem):
    src_txt = os.path.join(BASE_SRC, f"{stem}.txt")
    src_json = os.path.join(BASE_SRC, f"{stem}.json")
    dst_txt = os.path.join(BASE_DST, f"{stem}.txt")
    dst_json = os.path.join(BASE_DST, f"{stem}.json")

    # Skip if both exist
    if os.path.exists(dst_txt) and os.path.exists(dst_json):
        print(f"건너뜀: {stem}")
        return "skip"

    # --- Process TXT ---
    with open(src_txt, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    corrected_lines = []
    review_items = []
    for i, line in enumerate(lines):
        original = line.rstrip('\n')
        corrected = correct_line(original)
        corrected_lines.append(corrected)

    # Write corrected txt (preserve trailing newline behavior)
    with open(dst_txt, 'w', encoding='utf-8') as f:
        for i, line in enumerate(corrected_lines):
            if i < len(lines) - 1:
                f.write(line + '\n')
            else:
                # Last line: preserve original trailing newline
                if lines[-1].endswith('\n'):
                    f.write(line + '\n')
                else:
                    f.write(line)

    # --- Process JSON ---
    with open(src_json, 'r', encoding='utf-8') as f:
        data = json.load(f)

    segments = data.get('segments', [])
    for seg in segments:
        original_text = seg['text']
        corrected_text = correct_line(original_text)
        seg['text'] = corrected_text

    with open(dst_json, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Verify line count
    with open(dst_txt, 'r', encoding='utf-8') as f:
        dst_lines = f.readlines()
    if len(dst_lines) != len(lines):
        print(f"  경고: {stem} 줄 수 불일치 (원본={len(lines)}, 교정={len(dst_lines)})")

    print(f"완료: {stem}")
    return "done"

def main():
    os.makedirs(BASE_DST, exist_ok=True)
    stats = {"done": 0, "skip": 0}
    for stem in STEMS:
        result = process_stem(stem)
        stats[result] += 1
    print(f"\n처리 통계: 완료={stats['done']}, 건너뜀={stats['skip']}, 합계={stats['done']+stats['skip']}")

if __name__ == "__main__":
    main()
