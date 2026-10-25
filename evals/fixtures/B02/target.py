def binary_search(values, needle):
    low, high = 0, len(values) - 1
    while low < high:
        middle = (low + high) // 2
        if values[middle] == needle:
            return middle
        if values[middle] < needle:
            low = middle + 1
        else:
            high = middle - 1
    return -1
