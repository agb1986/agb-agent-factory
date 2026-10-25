# Usage

Create a store, add notes, and read them back in the order they were added.

```python
from notes.store import NoteStore

store = NoteStore()
index = store.add("buy milk")  # 0
store.all()  # ["buy milk"]
```

`all()` returns a copy, so changing the result does not change the store.
