"""bin/dispatch review 子命令能导入并调用 engine.agents.review（曾因导入旧的平铺模块名而崩溃）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.agents import dispatch, review


class DispatchReviewCommandTest(unittest.TestCase):
    def test_review_pr_reaches_the_review_module(self):
        with mock.patch.object(review, "review_pr", return_value=0) as review_pr:
            self.assertEqual(dispatch.main(["review", "14", "--reviewer", "opencode"]), 0)
        review_pr.assert_called_once_with(14, "opencode")

    def test_pending_and_watch_reach_the_review_module(self):
        with mock.patch.object(review, "review_pending", return_value=[3, 5]) as pending:
            self.assertEqual(dispatch.main(["review", "--pending"]), 0)
        pending.assert_called_once_with(None)
        with mock.patch.object(review, "watch", return_value=0) as watch:
            self.assertEqual(dispatch.main(["review", "--watch", "--interval", "2"]), 0)
        watch.assert_called_once_with(2.0, None)


if __name__ == "__main__":
    unittest.main()
