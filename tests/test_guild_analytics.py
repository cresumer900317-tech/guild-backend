import unittest
from datetime import date
from guild_analytics import captured_time, current_server_rows, growth_story, guild_dashboard, latest_members

TODAY=date(2026,9,14)
def member(name='A',power=200,stamp='2026-09-14T01:00:00+09:00'):
    return dict(name=name,power=power,guild='친구들',captured_at=stamp,power_text=str(power),level=2)
def point(day,name='A',power=100):
    return dict(name=name,power=power,snapshot_date=day,guild='친구들')

class AnalyticsTests(unittest.TestCase):
    def test_merge_uses_observation_time_not_largest_power(self):
        old=dict(nickname='A',power=300,server_rank=2,captured_at='2026-09-13T00:00:00Z')
        merged=current_server_rows([old],[member(power=200)])[0]
        self.assertEqual(merged['power'],200)
        self.assertEqual(merged['server_rank'],2)
        self.assertEqual(merged['rank_captured_at'],old['captured_at'])
        self.assertEqual(old['power'],300)
    def test_older_and_invalid_observations_cannot_override(self):
        old=dict(nickname='A',power=300,captured_at='2026-09-14T01:00:00Z')
        self.assertEqual(current_server_rows([old],[member()])[0]['power'],300)
        self.assertEqual(current_server_rows([old],[member(stamp='bad')])[0]['power'],300)
    def test_legacy_utc_and_nfc_deduplication(self):
        self.assertEqual(captured_time('2026-09-13T16:00:00').hour,16)
        import unicodedata
        rows=latest_members([member('군보',1),member(unicodedata.normalize('NFD','군보'),2,'2026-09-14T02:00:00+09:00')])
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['power'],2)
    def test_peak_and_goal_use_current_complete_roster(self):
        members=[member('A',200*10**16),member('B',75*10**16)]
        history=[point('2026-09-12','A',145*10**16)]
        story=growth_story(history,members,TODAY)
        self.assertEqual(story['goal']['target'],280*10**16)
        self.assertEqual(story['peak']['total'],275*10**16)
        self.assertEqual(story['days'],1)
        self.assertTrue(story['peak']['isToday'])
    def test_missing_dates_are_not_consecutive_growth(self):
        story=growth_story([point('2026-09-09',power=10),point('2026-09-12',power=20)], [member()],TODAY)
        self.assertEqual(story['streakDays'],0)
        self.assertEqual(story['comparisonDays'],2)
    def test_dashboard_fixed_cohort_and_missing_yesterday(self):
        members=[member('A'),member('B',400)]
        hist=[point('2026-09-01','A',100),point('2026-09-01','B',200),point('2026-09-10','A',150)]
        result=guild_dashboard(hist,members,TODAY)
        self.assertEqual(result['comparedMembers'],1)
        self.assertEqual([p['total'] for p in result['series']],[100,150,200])
        self.assertEqual(result['growthPct'],100)
        self.assertIsNone(result['growersYesterday'])
    def test_empty_and_single_observation_are_not_zero_growth(self):
        self.assertIsNone(guild_dashboard([],[],TODAY)['growthPct'])
        self.assertIsNone(growth_story([],[],TODAY)['peak']['total'])
        self.assertIsNone(guild_dashboard([],[member()],TODAY)['growthPct'])
    def test_former_member_is_not_counted(self):
        result=growth_story([point('2026-09-12','Former',9999),point('2026-09-12',power=100)],[member()],TODAY)
        self.assertEqual(result['peak']['total'],200)
        self.assertEqual(result['totalMembers'],1)
if __name__=='__main__': unittest.main()
