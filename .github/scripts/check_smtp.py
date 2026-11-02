"""Check SMTP authentication without sending an email or logging credentials."""
import os
import re
import smtplib
import sys


def main() -> int:
    config = os.environ.get("CUSTOM_CONFIG", "")
    server_match = re.search(r"^\s*smtp_server:\s*['\"]?([^\s'\"#]+)", config, re.MULTILINE)
    port_match = re.search(r"^\s*smtp_port:\s*['\"]?(\d+)", config, re.MULTILINE)
    sender = os.environ.get("SENDER", "")
    password = os.environ.get("SENDER_PASSWORD", "")
    if not (server_match and port_match and sender and password):
        print("SMTP host, port, sender, or authorization code is missing.", file=sys.stderr)
        return 1

    connection = None
    try:
        port = int(port_match.group(1))
        if port == 465:
            connection = smtplib.SMTP_SSL(server_match.group(1), port, timeout=30)
        else:
            connection = smtplib.SMTP(server_match.group(1), port, timeout=30)
            connection.starttls()
        connection.login(sender, password)
    except smtplib.SMTPAuthenticationError as exc:
        print(f"SMTP login was rejected (status {exc.smtp_code}).", file=sys.stderr)
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        print(f"SMTP login did not complete: {type(exc).__name__}.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                connection.quit()
            except (smtplib.SMTPException, OSError):
                connection.close()
    print("SMTP authentication succeeded. No email was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
