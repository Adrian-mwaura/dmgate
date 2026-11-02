import threading
import time

import pytest

from dmgate.parallel import parallel_map


def test_results_keep_the_order_of_the_items():
    def square(x):
        time.sleep(0.001 * (x % 3))  # finish out of order
        return x * x

    assert parallel_map(square, range(50), workers=8) == [x * x for x in range(50)]
    assert parallel_map(square, range(5)) == [0, 1, 4, 9, 16]


def test_the_work_really_runs_on_several_threads():
    seen, lock = set(), threading.Lock()

    def note(x):
        time.sleep(0.01)
        with lock:
            seen.add(threading.get_ident())
        return x

    parallel_map(note, range(16), workers=4)
    assert len(seen) > 1


def test_the_first_error_stops_the_items_not_yet_started():
    ran = []

    def work(x):
        if x == 0:
            raise ValueError("first")
        time.sleep(0.01)
        ran.append(x)
        return x

    with pytest.raises(ValueError, match="first"):
        parallel_map(work, range(1000), workers=4)
    assert len(ran) < 100
