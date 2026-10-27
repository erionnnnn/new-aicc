"""관리자 계정 복구(이 PC 에서만): 아이디 목록 확인·비밀번호 재설정.

  aicc-account list
  aicc-account reset <아이디>      (새 비밀번호를 두 번 입력)
"""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import select


def main(argv: list[str] | None = None) -> int:
    from .db import SessionLocal
    from .migrate import upgrade
    from .models import AuditLog, AuthSession, User
    from .security import hash_password

    args = sys.argv[1:] if argv is None else argv
    upgrade()
    with SessionLocal() as db:
        if not args or args[0] == "list":
            users = db.scalars(select(User).order_by(User.username)).all()
            if not users:
                print("등록된 계정이 없습니다. AICC 를 실행하면 처음 설정 화면이 나옵니다.")
            for u in users:
                role = {"admin": "관리자", "operator": "운영자", "reviewer": "검수자"}.get(u.role, u.role)
                print(f"{u.username}\t{role}\t{'사용' if u.active else '중지'}")
            return 0
        if args[0] == "reset" and len(args) == 2:
            u = db.scalar(select(User).where(User.username == args[1]))
            if u is None:
                print(f"'{args[1]}' 계정이 없습니다. 'aicc-account list' 로 아이디를 확인하세요.")
                return 1
            pw = getpass.getpass("새 비밀번호(10자 이상, 화면에 표시되지 않음): ")
            if len(pw) < 10 or pw != getpass.getpass("한 번 더: "):
                print("비밀번호가 10자 미만이거나 서로 다릅니다.")
                return 1
            u.password_hash = hash_password(pw)
            u.active = True
            for s in db.scalars(select(AuthSession).where(AuthSession.user_id == u.id)):
                db.delete(s)
            db.add(AuditLog(organization_id=u.organization_id, actor_type="local", actor_id=u.id,
                            action="user.password_reset_local", resource_type="user", resource_id=u.id, detail={}))
            db.commit()
            print(f"'{u.username}' 비밀번호를 바꿨습니다. 웹에서 다시 로그인하세요.")
            return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
