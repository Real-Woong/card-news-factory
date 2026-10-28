"""R_A_Glance — Instagram 채널 레이어.

이 패키지는 공통 엔진(``src/``)의 결과물을 받아 Instagram carousel 로 바꿉니다.
의존 방향은 한 방향입니다::

    R_A_Glance  →  Project_ContentOps(src/*)

공통 엔진은 이 패키지를 import 하지 않으며, 브랜드/디자인 설정을 알지 못합니다.
"""

__version__ = "0.1.0"
__channel__ = "instagram"
__brand__ = "R_A_Glance"
