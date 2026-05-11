"""
pipeline.py — Generalized greedy-matching pipeline for any farm.
"""

from datetime import date as _date

import polars as _pls
import pandas as pd
import numpy as np


def _as_date(d) -> _date:
    if isinstance(d, str):
        return _date.fromisoformat(d)
    return d

PROPS = ['p_Blood', 'p_Manure', 'p_Crack', 'p_Dirt', 'p_Damaged']
PROP_LABELS = {
    'p_Blood':   'Blood',
    'p_Manure':  'Manure',
    'p_Crack':   'Crack',
    'p_Dirt':    'Dirt',
    'p_Damaged': 'Damaged',
}

N_BINS_DEFAULT = 10
MIN_N          = 30


def greedy_match(df_count_house: pd.DataFrame, df_select_house: pd.DataFrame,
                 nbins: int, belt_duration: float) -> pd.DataFrame:
    dc = df_count_house.copy()
    dx = df_select_house.sort_values("Time").reset_index(drop=True).copy()

    if dc.empty or dx.empty:
        dx["Bin"] = np.nan
        return dx

    dc["EAT"] = dc["Time"] + belt_duration
    dc = dc.sort_values("EAT").reset_index(drop=True)
    dc["Bin"] = (
        np.floor(dc["Position"].fillna(0) * nbins / 100)
        .astype(int).clip(0, nbins - 1)
    )
    dc["Cumsum"] = dc["Eggs"].cumsum()

    total = int(dc.iloc[-1]["Cumsum"])
    if total == 0:
        dx["Bin"] = np.nan
        return dx

    factor = len(dx) / total
    dc["Xi"] = (np.around(factor * (dc["Cumsum"] - dc["Eggs"] + 1)) - 1).astype(int).clip(0)
    dc["Xe"] = (np.around(factor * dc["Cumsum"]) - 1).astype(int).clip(0, len(dx) - 1)

    dx["Bin"] = np.nan
    for _, row in dc.iterrows():
        dx.loc[row["Xi"]:row["Xe"], "Bin"] = row["Bin"]

    return dx
