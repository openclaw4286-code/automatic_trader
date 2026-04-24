"""Backtesting package for the ICT bot.

Three pieces:
 * data   — historical OHLCV cache (Gate.io → parquet)
 * engine — single-config replay over cached data, simulating
            entry/TP1/TP2/SL/trailing in R-units
 * grid   — sweep multiple parameter combinations and rank by
            (signal_count × expectancy_R), with Pareto front
"""
