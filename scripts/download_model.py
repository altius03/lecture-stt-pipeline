from __future__ import annotations

from faster_whisper import WhisperModel


# 모델 로딩이 가능한지 빠르게 확인하는 간단한 점검 스크립트


def main() -> None:
    # WhisperModel 인스턴스를 초기화해 환경이 정상 동작하는지 검증한다.
    model = WhisperModel(
        "large-v3",
        device="cpu",
        compute_type="int8",
    )
    print("Model ready:", model)


if __name__ == "__main__":
    main()
