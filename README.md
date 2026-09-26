# memefwd — 밈코인 필터 포워드 테스트 (페이퍼, 실매수 없음)

@savipww 가이드의 하드필터·체인필터를 재현해서, 필터를 통과한 토큰(pass)과 탈락한 토큰(control)의 이후 수익률과 러그 비율을 2주간 비교합니다.
LLM(Jev) 판단은 뺐습니다. 하드필터에 엣지가 없으면 그 위에 LLM을 얹을 근거도 없기 때문입니다.

## Railway 배포

1. 이 폴더를 GitHub 새 저장소에 올린 뒤 Railway에서 **New Project → Deploy from GitHub repo**를 선택합니다.
   (또는 이 폴더에서 `railway init` → `railway up`)
2. 서비스 우클릭 → **Attach Volume**을 선택하고, Mount path를 `/data`로 지정합니다.
   **이 단계를 빠뜨리면 재배포할 때마다 데이터가 날아갑니다.**
3. Variables에 추가합니다.
   - `REPORT_TOKEN` = 아무 긴 문자열 (리포트 URL 보호용)
   - (선택) `NETWORKS=solana,bsc,robinhood`, `GT_RPM=9`
4. Settings → Networking → **Generate Domain**을 누릅니다.
5. 리포트 확인
   - `https://<도메인>/?key=<REPORT_TOKEN>` : 리포트
   - `https://<도메인>/export.csv?key=<REPORT_TOKEN>` : 원자료 CSV

## 동작

- 15분마다 GeckoTerminal에서 체인별 신규 풀 40개를 가져와 관찰 목록에 등록합니다.
- 각 풀은 상장 후 20분, 45분, 1.5h, 3h, 6h, 12h, 24h, 48h, 72h 시점에 재평가합니다.
- 평가 순서
  1. 하드필터: 나이, 유동성, 거래량, 시총, 거래수, 매도 없음 여부
  2. 체인필터: 홀더, top10, top지갑, 권한, 허니팟
- 처음으로 통과한 시점의 가격을 pass 진입가로 기록하고, 24시간 동안 15분 간격으로 추적합니다.
- 처음 평가 가능한 나이에 탈락한 토큰의 35%를 대조군(control)으로 삼아, 1h·6h·24h 시점 가격을 기록합니다.
- 리포트에는 사전 합격 기준 C1~C4가 자동으로 판정되어 표시됩니다. 기준은 시작 전에 고정한 것이니 결과를 보고 바꾸지 않습니다.

## 알려진 한계

- 진입가는 GeckoTerminal 중간가입니다. 실제 체결 슬리피지는 왕복비용 3% 가정으로만 반영했습니다.
- 가격 추적 해상도는 15분입니다. 그 사이에 일어난 급락은 보이지 않으므로 손절 시뮬레이션은 낙관적입니다.
- `robinhood` 네트워크 ID가 GeckoTerminal에서 다르면 자동으로 제외되고, 리포트에 경고가 표시됩니다.
