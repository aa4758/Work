# 미국 후보군 선정 규칙 (`screen-us`)

전략은 **매수가 대비 -5% 손절 / +15% 익절**입니다. 손익비가 1:3이라 익절이 손절보다 먼저 나올 확률이 **25%를 넘어야** 기대수익이 플러스입니다.
그래서 후보군 규칙은 "좋은 회사"보다 **"흔들려서 손절에 걸리지 않고, 추세가 이어져 +15%에 닿을 종목"**을 찾도록 설계했습니다.

실행: `python Investment/agent_tools/market_data.py screen-us --top 30` (저장소 루트, 약 30초)

## 1. 대상 범위
**S&P500 구성종목** 약 503개. 업종 분류는 GICS 섹터(Wikipedia 기준)를 씁니다.
나스닥100 종목은 대부분 S&P500에도 들어 있고, 빠진 것은 해외 기업 ADR 등 일부입니다.

## 2. 탈락 필터 (순서대로 적용)

| # | 규칙 | 기준 | 근거 |
|---|---|---|---|
| F1 | 이력 | 273거래일 이상 | 12-1 모멘텀 계산에 필요 |
| F2 | 가격 | $10 이상 | 저가주는 호가 단위와 잡음이 커서 손절이 제값에 체결되지 않음 |
| F3 | 유동성 | 20일 거래대금 중앙값 **$5천만 이상** | 유동성이 낮은 종목은 거래비용과 가격 충격이 큼 (Amihud 2002). 손절 주문의 슬리피지를 줄임 |
| F4 | 변동성 | **ATR14가 주가의 1.5~3.0%** | 손절폭 5%가 ATR의 1.7~3.3배가 됨. 실무에서는 손절을 ATR의 2~3배에 두어 일상 변동에 걸리지 않게 함 (Wilder 1978; Kaufman *Trading Systems and Methods*). 1.5% 미만이면 +15%까지 너무 오래 걸림 |
| F5 | 추세 | 종가 > 200일선 **그리고** 50일선 > 200일선 | 이동평균 추세 규칙의 초과성과 (Brock·Lakonishok·LeBaron 1992; Faber 2007). 자기 가격 추세의 지속성 (Moskowitz·Ooi·Pedersen 2012) |
| F6 | 복권형 종목 제외 | 최근 21일 **최대 일간수익률 상위 10%** 제외 | 급등일이 있었던 종목은 이후 수익률이 낮음 (Bali·Cakici·Whitelaw 2011, "MAX 효과"). 이벤트성 급등 종목의 되돌림으로 손절될 위험을 줄임 |

## 3. 순위 점수 (살아남은 종목끼리 백분위, 동일가중)

| 팩터 | 계산 | 근거 |
|---|---|---|
| 12-1 모멘텀 | 12개월 전부터 1개월 전까지의 수익률 (최근 1개월 제외) | 지난 3~12개월의 승자가 계속 오름 (Jegadeesh·Titman 1993). 최근 1개월은 단기 반전 효과 때문에 뺌 (Jegadeesh 1990) |
| 52주 고가 근접도 | 종가 ÷ 52주 최고가 | 고가 근처 종목은 투자자의 기준점 편향으로 호재가 늦게 반영되어 계속 오름 (George·Hwang 2004) |
| 정보 불연속성 (낮을수록 좋음) | sign(모멘텀) × (하락일 비율 − 상승일 비율) | 급등 며칠보다 꾸준히 조금씩 오른 모멘텀이 더 오래 지속됨 ("frog-in-the-pan", Da·Gurun·Warachka 2014) |

세 팩터는 **같은 비중**으로 합칩니다. 가중치를 과거 데이터에 맞춰 조정하면 과최적화가 되기 쉬워서, 단순한 동일가중이 표본 밖에서 더 안정적인 경우가 많습니다 (DeMiguel·Garlappi·Uppal 2009).

**손절 규칙과 잘 맞는 이유:** 손절은 수익률에 추세 지속성(양의 자기상관)이 있을 때 성과를 높입니다 (Kaminski·Lo 2014). 모멘텀 종목에 손절을 결합하면 모멘텀 전략의 큰 손실이 줄어듭니다 (Han·Zhou·Zhu 2016). 그래서 순위 점수를 추세 지속성 팩터로만 구성했습니다.

## 4. 분산
한 GICS 섹터는 **후보군의 20%까지만** 담습니다 (30개면 섹터당 6개). 최종 10종목에서는 결정권자가 업종당 3개 상한을 추가로 적용합니다.

## 5. 시장 국면 경고
S&P500 지수가 200일선 아래면 **"약세"**로 표시합니다. 약세장에서 반등이 올 때 모멘텀 전략이 크게 무너지는 경향이 있기 때문입니다 (Daniel·Moskowitz 2016). 이때 결정권자는 편입 종목 수를 줄이고 현금 슬롯을 검토합니다.

## 6. 이 도구가 하지 않는 것 (다음 단계 에이전트가 확인)
- **실적발표 일정:** 발표일에는 주가가 크게 뛰거나 빠질 수 있어 손절선 아래에서 체결될 수 있습니다. 뉴스 분석가와 결정권자가 보유 예상기간 안의 발표일을 확인합니다. 발표 뒤 서프라이즈 방향으로 주가가 계속 흐르는 경향(PEAD, Ball·Brown 1968; Bernard·Thomas 1989)은 뉴스 분석가가 가점으로 반영합니다.
- **재무 품질:** 매출총이익/자산(Novy-Marx 2013)과 수익성·안정성 종합(Quality Minus Junk, Asness·Frazzini·Pedersen 2019)은 기업가치 분석가가 확인합니다.

## 한계
- 연구 결과는 과거 평균이며, 발표 이후 팩터 효과는 약해지는 경향이 있습니다 (McLean·Pontiff 2016).
- 대부분의 연구는 월 단위 리밸런싱을 전제로 하지만, 이 전략은 손절·익절로 보유기간이 정해집니다. 규칙의 문턱값(ATR 1.5~3.0%, $5천만 등)은 이 전략에 맞춘 **설계 선택**이고, 논문에서 그대로 가져온 값이 아닙니다.
- 현재 구성종목만 쓰므로 과거 백테스트에는 생존자 편향이 있습니다.

## 참고문헌
- Amihud, Y. (2002). Illiquidity and stock returns. *Journal of Financial Markets*.
- Asness, C., Frazzini, A., Pedersen, L. (2019). Quality minus junk. *Review of Accounting Studies*.
- Bali, T., Cakici, N., Whitelaw, R. (2011). Maxing out: Stocks as lotteries and the cross-section of expected returns. *Journal of Financial Economics*.
- Ball, R., Brown, P. (1968). An empirical evaluation of accounting income numbers. *Journal of Accounting Research*.
- Bernard, V., Thomas, J. (1989). Post-earnings-announcement drift. *Journal of Accounting Research*.
- Brock, W., Lakonishok, J., LeBaron, B. (1992). Simple technical trading rules and the stochastic properties of stock returns. *Journal of Finance*.
- Da, Z., Gurun, U., Warachka, M. (2014). Frog in the pan: Continuous information and momentum. *Review of Financial Studies*.
- Daniel, K., Moskowitz, T. (2016). Momentum crashes. *Journal of Financial Economics*.
- DeMiguel, V., Garlappi, L., Uppal, R. (2009). Optimal versus naive diversification. *Review of Financial Studies*.
- Faber, M. (2007). A quantitative approach to tactical asset allocation. *Journal of Wealth Management*.
- George, T., Hwang, C. (2004). The 52-week high and momentum investing. *Journal of Finance*.
- Han, Y., Zhou, G., Zhu, Y. (2016). Taming momentum crashes: A simple stop-loss strategy. Working paper.
- Jegadeesh, N. (1990). Evidence of predictable behavior of security returns. *Journal of Finance*.
- Jegadeesh, N., Titman, S. (1993). Returns to buying winners and selling losers. *Journal of Finance*.
- Kaminski, K., Lo, A. (2014). When do stop-loss rules stop losses? *Journal of Financial Markets*.
- McLean, R., Pontiff, J. (2016). Does academic research destroy stock return predictability? *Journal of Finance*.
- Moskowitz, T., Ooi, Y., Pedersen, L. (2012). Time series momentum. *Journal of Financial Economics*.
- Novy-Marx, R. (2013). The other side of value: The gross profitability premium. *Journal of Financial Economics*.
- Wilder, J. W. (1978). *New Concepts in Technical Trading Systems*.
