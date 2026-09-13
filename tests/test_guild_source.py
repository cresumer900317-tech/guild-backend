import unittest
from guild_source import parse_guild_source,korean_number

HTML='''<span class="guild-name">친구들</span><div class="guild-update-row">📅 2026.09.12 기준 데이터</div><div class="guild-stats"><div class="metric-bp"><span class="power-tooltip">270경 4590조</span></div><div class="metric-gb"><span class="power-tooltip">37,314,037,379,851</span></div></div><div class="members-list"><div class="member-row" data-bp="1730163011352299008" data-gb="11344634830891"><a class="nick-link">군보</a><div class="member-sub">비숍 | Lv.125</div></div><div class="member-row" data-bp="100"><a class="nick-link">점수없음</a><div class="member-sub">팔라딘 | Lv.10</div></div></div>'''
class SourceTests(unittest.TestCase):
 def test_source_date_and_precise_values(self):
  r=parse_guild_source(HTML,'친구들')
  self.assertEqual(r['sourceDate'],'2026-09-12');self.assertEqual(r['members'][0]['power'],1730163011352299008)
  self.assertEqual(r['members'][0]['bossScore'],11344634830891);self.assertEqual(r['members'][0]['job'],'비숍')
 def test_missing_score_is_not_zero(self):
  self.assertIsNone(parse_guild_source(HTML,'친구들')['members'][1]['bossScore'])
  self.assertEqual(korean_number('0'),0);self.assertIsNone(korean_number('-'))
 def test_challenge_or_wrong_guild_rejected(self):
  with self.assertRaises(ValueError):parse_guild_source('<title>Access denied</title>','친구들')
  with self.assertRaises(ValueError):parse_guild_source(HTML,'친구둘')
if __name__=='__main__':unittest.main()
