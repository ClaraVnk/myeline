#!/usr/bin/env python3
"""Script to audit log statements for GDPR compliance.

This script scans Python files for logger statements that may contain
personal data (emails) and suggests corrections.

Usage:
    python scripts/audit_logs.py
    python scripts/audit_logs.py --fix  # Auto-fix simple cases
"""

import os
import re
import sys
from pathlib import Path


def find_log_statements_with_emails(file_path):
    """Find logger statements that may contain email addresses.

    Args:
        file_path (str): Path to Python file to analyze

    Returns:
        list: List of tuples (line_number, line_content, issue_type)
    """
    issues = []

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    for i, line in enumerate(lines, 1):
        # Skip comments
        if line.strip().startswith('#'):
            continue

        # Check for logger statements
        if 'logger.' in line and any(level in line for level in ['info', 'debug', 'warning', 'error']):
            # Check for .email attribute
            if '.email' in line:
                issues.append((i, line.strip(), 'email_attribute'))
            # Check for email variable
            elif re.search(r'\bemail\b', line) and 'f"' in line or 'f\'' in line:
                issues.append((i, line.strip(), 'email_variable'))
            # Check for user.email pattern
            elif re.search(r'user\.email|current_user\.email', line):
                issues.append((i, line.strip(), 'user_email'))

    return issues


def suggest_fix(line, issue_type):
    """Suggest a fix for a problematic log statement.

    Args:
        line (str): Original line
        issue_type (str): Type of issue

    Returns:
        str: Suggested fix
    """
    if issue_type in ['email_attribute', 'user_email']:
        # Replace user.email with user.id
        fixed = re.sub(r'(\w+)\.email', r'\1.id', line)
        return None, fixed
    elif issue_type == 'email_variable':
        fixed = re.sub(r'\{email\}', r'{user_id}', line)
        return None, fixed

    return None, line


def audit_directory(directory='app'):
    """Audit all Python files in a directory for GDPR log compliance.

    Args:
        directory (str): Directory to audit
    """
    print(f"🔍 Auditing logs in {directory}/ for GDPR compliance...\n")

    total_issues = 0
    files_with_issues = []

    for root, dirs, files in os.walk(directory):
        # Skip venv, __pycache__, etc.
        dirs[:] = [d for d in dirs if d not in ['venv', '__pycache__', '.git', 'node_modules']]

        for file in files:
            if file.endswith('.py'):
                file_path = os.path.join(root, file)
                issues = find_log_statements_with_emails(file_path)

                if issues:
                    files_with_issues.append(file_path)
                    print(f"📄 {file_path}")
                    print("=" * 80)

                    for line_num, line, issue_type in issues:
                        total_issues += 1
                        print(f"  Line {line_num}: {issue_type}")
                        print(f"  ❌ {line}")

                        import_line, fixed = suggest_fix(line, issue_type)
                        if fixed != line:
                            if import_line:
                                print(f"  💡 Add import: {import_line.strip()}")
                            print(f"  ✅ Suggested: {fixed}")
                        print()

                    print()

    # Summary
    print("=" * 80)
    print("\n📊 Summary:")
    print(f"  Files scanned: {sum(1 for _ in Path(directory).rglob('*.py'))}")
    print(f"  Files with issues: {len(files_with_issues)}")
    print(f"  Total issues found: {total_issues}")

    if total_issues > 0:
        print(f"\n⚠️  {total_issues} log statement(s) may contain personal data (emails)")
        print("   Please review and replace .email references with .id in log statements")
        return 1
    else:
        print("\n✅ No issues found! All logs appear GDPR-compliant.")
        return 0


def main():
    """Main function."""
    if len(sys.argv) > 1 and sys.argv[1] == '--help':
        print(__doc__)
        return 0

    return audit_directory('app')


if __name__ == '__main__':
    sys.exit(main())
