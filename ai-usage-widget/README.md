# AI Usage Mini Widget KR (통합 전환형)

Codex(ChatGPT)와 Gemini(Antigravity) 사용량을 하나의 작은 창에서 번갈아 확인하는 한국어 Windows 미니 위젯입니다.

## 주요 기능

- **단일 창 + 전환형 표시**: 기존 위젯과 같은 1줄 크기를 유지한 채 Codex와 Gemini를 백그라운드에서 각각 1분마다 독립 조회합니다.
- **세 가지 전환 방식**:
  - **자동 전환**: 기본값 10초 간격으로 Codex ↔ Gemini를 자동 순환합니다. 우클릭 메뉴의 `자동 전환` 슬라이더로 `끄기(0), 1초, 3초, 5초, 10초, 30초` 중 선택할 수 있으며, 메뉴가 열려 있거나 초기화권 처리 중에는 자동 전환을 잠시 멈춥니다.
  - **마우스 가운데 버튼(휠 클릭)**: 위젯 위를 가운데 버튼으로 클릭하면 즉시 다른 서비스로 전환하고 자동 전환 타이머를 초기화합니다.
  - **우클릭 메뉴 상단**: `전환: Gemini 보기` / `전환: Codex 보기` 항목으로 수동 전환할 수 있습니다.
- **사용량 기반 색상 통일**: Codex와 Gemini 모두 사용률(%)에 따라 `5H`·`WK` 라벨, 퍼센트, 5칸 게이지 색상이 동일한 그라데이션으로 변합니다.
- **기존 위젯 기능 통합 제공**:
  - 좌클릭 통과(click-through), 우클릭 메뉴 및 우클릭 드래그 이동, 이동 잠금, 투명도(0~100%, 10% 단위 슬라이더), 호버 투명도 80%, 항상 맨 위 표시, Windows 시작 시 자동 실행.
  - 우클릭 메뉴에서 **종료**를 눌러 직접 종료하면 Windows 시작 시 자동 실행도 함께 해제됩니다. PC 종료·재부팅으로 위젯이 닫힐 때는 자동 실행이 유지됩니다.
  - Codex 화면 우클릭 메뉴에서 계정·요금제·크레딧·초기화권 확인 및 사용 가능.
  - Gemini 화면 우클릭 메뉴에서 계정·요금제·크레딧·Claude/GPT 공유 풀(5시간·주간) 확인 가능.

## 실행

Windows 10/11, Tkinter를 포함한 Python 3.10 이상, 그리고 로그인된 Codex 또는 실행 중인 Antigravity가 필요합니다.

저장소 루트의 `../start_ai_widget.bat`를 실행하거나 아래 명령으로 실행합니다.

```powershell
python widget.py
```

## 표시와 크기

- 넥슨 풋볼고딕을 사용하며, 사용량은 Bold 9pt, 초기화 시간은 Light 9pt로 표시합니다.
- 위젯 왼쪽에 현재 표시 중인 서비스의 로고(`codex-logo.png` / `gemini-logo.png`)를 표시합니다.
- 위젯은 최소 너비 270px·높이 38px이며, 서비스 전환 시 창 너비가 변하지 않고 일정하게 유지됩니다.

## 저장 파일

- 설정: `%LOCALAPPDATA%\AIUsageMini\settings.json` (투명도·호버 투명도·위치·이동 잠금·마지막 선택 서비스·자동 전환 간격)
- Codex 미확정 초기화권 기록: `%LOCALAPPDATA%\CodexUsageMini\pending-reset.json` (기존 Codex 위젯과 안전 기록을 공유)

## 라이선스

[Wannatep/codex-usage-mini-widget](https://github.com/Wannatep/codex-usage-mini-widget)을 기반으로 하며, 원본의 [The Unlicense](../UNLICENSE)를 유지합니다.

동봉한 넥슨 풋볼고딕의 [저작권·이용 조건](../common/assets/fonts/NEXON-LICENSE.txt)과 [출처 안내](../common/assets/fonts/README.md)를 함께 제공합니다.
