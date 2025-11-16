#!/usr/bin/env python3
import re

# Read the file
with open('src/main_live.py', 'r', encoding='utf-8') as f:
    content = f.read()
    lines = content.split('\n')

# Fix line 1677 - change from 3 tabs to 2 tabs
if len(lines) > 1676 and lines[1676].startswith('\t\t\t\texcept KeyboardInterrupt:'):
    lines[1676] = '\t\t\texcept KeyboardInterrupt:'
elif len(lines) > 1676 and lines[1676].startswith('\t\t\texcept KeyboardInterrupt:'):
    lines[1676] = '\t\texcept KeyboardInterrupt:'

# Fix line 1682
if len(lines) > 1681 and lines[1681].startswith('\t\t\t\texcept (ConnectionError)'):
    lines[1681] = '\t\t\texcept (ConnectionError) as e:'
elif len(lines) > 1681 and lines[1681].startswith('\t\t\texcept (ConnectionError)'):
    lines[1681] = '\t\texcept (ConnectionError) as e:'

# Fix line 1687
if len(lines) > 1686 and lines[1686].startswith('\t\t\t\texcept Exception'):
    lines[1686] = '\t\t\texcept Exception as e:'
elif len(lines) > 1686 and lines[1686].startswith('\t\t\texcept Exception'):
    lines[1686] = '\t\texcept Exception as e:'

# Write back
with open('src/main_live.py', 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print("Fixed indentation!")



