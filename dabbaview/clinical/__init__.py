# Copyright (c) 2026 Park Seongho (Dabbabbu)
# This file is part of DabbaView, licensed under GPL-3.0.
# See LICENSE for details.
"""
전문 분석 도구 (Analysis 메뉴) - 심장 · 신경 · 종양 · 폐 · 근골격 · 혈관 · 확산 · 관류 · MRS

계산 엔진(data, models, cardiac, lesion, mrs)은 UI와 분리되어 있고,
UI는 panel.AnalysisDock 한 곳에서 도구별 페이지로 보여 준다.

주의: 연구·교육용 구현이며 진단용으로 검증(인허가)된 소프트웨어가 아니다.
"""

DISCLAIMER = ("연구·교육용 계산입니다. 진단용으로 검증된 소프트웨어가 아니므로 "
              "결과는 원래 기록 워크스테이션·검증된 도구와 대조해서 쓰세요.")
