"""Parse the official typed nutrition table; never infer portions or nutrients."""
import csv
import math
import re


def parse_table(value):
    if not isinstance(value,str):raise ValueError('official_nutrition_format')
    lines=value.strip().splitlines()
    if not lines:raise ValueError('official_nutrition_empty')
    header=re.fullmatch(r'\[\d+\]\{([^}]+)\}:',lines[0].strip())
    if not header:raise ValueError('official_nutrition_header')
    columns=next(csv.reader([header[1]]));items=[]
    for parts in csv.reader(lines[1:],skipinitialspace=True):
        if len(parts)!=len(columns):continue
        row=dict(zip(columns,parts));name=row.get('productName','').strip()
        try:
            nutrients={key:float(row[source]) for key,source in [('kcal','energyKcal'),('protein_g','protein'),('fat_g','fat'),('carb_g','carbohydrate')]}
            if not name or any(not math.isfinite(x) or x<0 for x in nutrients.values()):continue
        except (ValueError,KeyError):continue
        description=row.get('nutritionDescription','').strip()
        items.append({'name':name,'portion':description if description not in ('null','') else '',**nutrients})
    if not items:raise ValueError('official_nutrition_no_valid_rows')
    return items
