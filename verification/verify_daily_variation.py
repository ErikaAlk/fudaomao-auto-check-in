from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'t6'))
WORK = ROOT/'.verification'
WORK.mkdir(exist_ok=True)


def main():
    with tempfile.TemporaryDirectory(dir=WORK) as directory:
        os.environ['LZU_MANAGER_HOME'] = directory
        import manager_store as store
        import attendance

        store.initialize()
        day = '2026-11-15'
        job = {'run_at':'21:05', 'deadline':'23:50', 'start_date':day, 'weekdays':'[1,2,3,4,5,6,7]'}
        zone = timezone(timedelta(hours=8))
        first = datetime.fromisoformat(day+'T21:05:00').replace(tzinfo=zone)
        identifiers = [store.identifier() for _ in range(4)]
        saved = {}
        with store.connect() as conn:
            for job_id in identifiers:
                saved[job_id] = store.daily_variation(conn, job_id, day)
        with store.connect() as conn:
            for job_id in identifiers:
                assert store.daily_variation(conn, job_id, day) == saved[job_id]
                following = store.daily_variation(conn, job_id, '2026-11-16')
                assert following['day'] == '2026-11-16'
                assert (following['radial_fraction'],following['bearing_fraction']) != (saved[job_id]['radial_fraction'],saved[job_id]['bearing_fraction'])
            assert conn.execute('SELECT count(*) FROM daily_variations').fetchone()[0] == 8
        for delay in range(121):
            start = store.varied_start(job, day, {'delay_seconds':delay})
            assert (start-first).total_seconds() == delay
            cfg = {**job, 'run_at':start.strftime('%H:%M:%S')}
            assert attendance.schedule_gate(cfg, start-timedelta(seconds=1)) == 'before_window'
            assert attendance.schedule_gate(cfg, start) is None
            assert attendance.schedule_gate(cfg, first.replace(hour=23,minute=50)) == 'window_closed'
            short = store.varied_start({**job,'deadline':'21:05:30'},day,{'delay_seconds':delay})
            assert first <= short < first+timedelta(seconds=30)
            for phase in (0,1):
                ticks = [first+timedelta(seconds=second) for second in range(phase,30,2)]
                assert any(short <= tick < first+timedelta(seconds=30) for tick in ticks)
        for latitude in (-89.9,-45,0,35,89.9):
            location = {'latitude':latitude,'longitude':104.0,'coordinate_system':'BD-09','address':'验证位置'}
            for radius in (0.01,0.1,1,20,50,500):
                detail = {'signPlaceSelected':[{'latitude':latitude,'longitude':104.0,'radius':radius}]}
                clearance = attendance.validate_location(detail,location)
                for _ in range(40):
                    variation = {'radial_fraction':secrets.randbelow(2**53)/2**53,
                                 'bearing_fraction':secrets.randbelow(2**53)/2**53}
                    candidate = attendance.varied_location(location,variation,clearance)
                    assert attendance.distance_m(104.0,latitude,candidate['longitude'],candidate['latitude']) <= 50
                    assert candidate['address'] == location['address']
                    attendance.validate_location(detail,candidate)
            fence = {'signPlaceSelected':[{'latitude':latitude,'longitude':104.0,'radius':100}]}
            near = attendance.varied_location(location,{'radial_fraction':0.999999,'bearing_fraction':0.25},100)
            margin = attendance.validate_location(fence,near)
            candidate = attendance.varied_location(near,{'radial_fraction':0.999999,'bearing_fraction':0.25},margin)
            attendance.validate_location(fence,candidate)
        assert (saved[identifiers[0]]['radial_fraction'],saved[identifiers[0]]['bearing_fraction']) != (saved[identifiers[1]]['radial_fraction'],saved[identifiers[1]]['bearing_fraction'])
    print('每日参数隔离与重开数据库保持、121个延迟边界、短窗口、1200个位置与学校范围检查通过；未访问学校或发送通知。')


if __name__ == '__main__':
    main()
