from __future__ import annotations

from faster_whisper import WhisperModel


def main() -> None:
    model = WhisperModel(
        "large-v3",
        device="cpu",
        compute_type="int8",
    )
    print("Model ready:", model)


if __name__ == "__main__":
    main()
