from app.services.system_diagnostics import build_system_diagnostics


def main() -> int:
    diagnostics = build_system_diagnostics()
    readiness = diagnostics["deployment_readiness"]

    print(f"Staging readiness for ENVIRONMENT={readiness['target_environment']}")
    print(f"Checks: {readiness['checks_ok']}/{readiness['checks_total']}")
    print()
    for item in readiness["checks"]:
        print(f"[{item['status']}] {item['name']}: {item['detail']}")
    print()
    print("Next steps:")
    for step in readiness["next_steps"]:
        print(f"- {step}")

    return 0 if readiness["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
