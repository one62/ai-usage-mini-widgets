# AI Usage Mini Widgets

Codex(ChatGPT)와 Gemini(Antigravity) 사용량을 바탕화면에 작게 띄워 확인하는 Windows 미니 위젯 모음입니다.

## Windows 실행 파일

[GitHub Releases](https://github.com/one62/ai-usage-mini-widgets/releases)의 **Assets**에서 필요한 Windows x64 실행 파일을 다운로드하세요. Python은 실행 파일에 포함되어 있습니다.

- `CodexUsageWidget.exe` — 로그인된 Codex 데스크톱 앱 또는 CLI 필요
- `GeminiUsageWidget.exe` — 로그인 후 실행 중인 Antigravity 필요
- `AIUsageWidget.exe` — Codex와 Gemini를 한 창에서 전환하며 확인하는 통합 위젯 ([상세 안내](ai-usage-widget/README.md))

자동 실행을 사용하려면 파일을 계속 둘 폴더로 옮긴 뒤, 위젯 우클릭 메뉴에서 **Windows 시작 시 자동 실행**을 켜세요. PC 종료·재부팅 등으로 위젯이 닫힐 때는 자동 실행이 유지되지만, 우클릭 메뉴에서 **종료**를 눌러 직접 종료하면 **Windows 시작 시 자동 실행도 함께 해제**됩니다. 업데이트하거나 파일을 옮긴 뒤에는 새 실행 파일에서 위젯을 열고 자동 실행을 다시 켜세요. 위젯 설정(위치·투명도 등)은 그대로 유지됩니다.

## 폴더 구성

- `common/`: 세 위젯이 함께 사용하는 설정 저장(`settings.py`), 색상·시간 포맷(`formatting.py`), Win32 유틸(`win32.py`), 우클릭 메뉴(`popup_menu.py`), 공통 베이스 위젯(`widget_base.py`), 넥슨 풋볼고딕 폰트(`assets/fonts/`)
- `codex-usage-widget/`: Codex 사용량 미니 위젯 ([상세 안내](codex-usage-widget/README.md))
- `gemini-usage-widget/`: Gemini / Antigravity 사용량 미니 위젯 ([상세 안내](gemini-usage-widget/README.md))
- `ai-usage-widget/`: Codex·Gemini 단일 창 전환형 통합 위젯 ([상세 안내](ai-usage-widget/README.md))

## 소스에서 실행 및 재시작

루트 폴더의 배치 파일을 실행하면 해당 위젯을 실행합니다. 이미 같은 위젯이 실행 중이면 기존 프로세스를 종료한 뒤 다시 시작합니다.

- Codex 위젯: `start_codex_widget.bat`
- Gemini 위젯: `start_gemini_widget.bat`
- 통합 전환 위젯: `start_ai_widget.bat`

직접 Python으로 실행할 수도 있습니다.

```powershell
python codex-usage-widget\widget.py
python gemini-usage-widget\widget.py
python ai-usage-widget\widget.py
```

## 테스트

저장소 루트에서 전체 단위 테스트를 한 번에 실행할 수 있습니다.

```powershell
python -m unittest common/tests/test_common.py codex-usage-widget/tests/test_codex_widget.py gemini-usage-widget/tests/test_gemini_widget.py ai-usage-widget/tests/test_ai_widget.py -v
```

## 라이선스

[Wannatep/codex-usage-mini-widget](https://github.com/Wannatep/codex-usage-mini-widget)을 기반으로 하며, 루트의 [The Unlicense](UNLICENSE)를 유지합니다.

동봉한 넥슨 풋볼고딕의 [저작권·이용 조건](common/assets/fonts/NEXON-LICENSE.txt)과 [출처 안내](common/assets/fonts/README.md)를 함께 제공합니다.
