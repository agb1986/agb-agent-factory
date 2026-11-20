# File format

One `key=value` pair per line. Spaces around the key and the value are dropped. A line with no
`=` makes `load()` raise `ValueError` with the line number.
