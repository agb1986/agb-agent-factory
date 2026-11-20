# textkit

Small helpers for cleaning up text.

`words(text)` splits text into lowercase words and drops punctuation:

```python
from textkit.words import words

words("Hello, World!")  # ["hello", "world"]
```
