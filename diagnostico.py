from datetime import datetime

import report_automation as ra

if __name__ == "__main__":
    conn_str = ra.get_connection_string()
    df = ra.fetch_import_data(conn_str)
    diagnostico = ra.debug_filter_pipeline(df, datetime.now().date())
    print(diagnostico)
