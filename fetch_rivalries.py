import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cursor = conn.cursor()

cursor.execute('DELETE FROM rivalries')

rivalries = [
    # Air Force
    ("Air Force","Army","Commander's Classic"),
    ("Air Force","Colorado State","Battle for the Ram-Falcon"),
    ("Air Force","Hawaii","Battle for the Kuter"),
    ("Air Force","Navy","Air Force-Navy Game"),
    # Akron
    ("Akron","Kent State","Battle for the Wagon Wheel"),
    # Alabama
    ("Alabama","Auburn","Iron Bowl"),
    ("Alabama","Mississippi State","Battle for Highway 82"),
    ("Alabama","Tennessee","Third Saturday in October"),
    # Appalachian State
    ("Appalachian State","Charlotte",""),
    ("Appalachian State","Georgia Southern","Deeper than Hate"),
    # Arizona
    ("Arizona","Arizona State","Duel in the Desert"),
    # Arkansas
    ("Arkansas","LSU","Battle for the Golden Boot"),
    ("Arkansas","Missouri","Battle Line"),
    ("Arkansas","Texas A&M","Southwest Classic"),
    # Arkansas State
    ("Arkansas State","Memphis","Paint Bucket Bowl"),
    # Army
    ("Army","Navy","Army-Navy Game"),
    ("Army","Notre Dame",""),
    # Auburn
    ("Auburn","Florida",""),
    ("Auburn","Georgia","Deep South's Oldest Rivalry"),
    # Ball State
    ("Ball State","Miami (OH)","Red Bird Rivalry"),
    ("Ball State","Northern Illinois","Battle for the Bronze Stack"),
    # Baylor
    ("Baylor","TCU",""),
    ("Baylor","Texas A&M","Battle of the Brazos"),
    ("Baylor","Texas Tech",""),
    # Boise State
    ("Boise State","Fresno State","Battle for the Milk Can"),
    # Boston College
    ("Boston College","Clemson","Battle for the O'Rourke-McFadden Trophy"),
    ("Boston College","Notre Dame","Frank Leahy Memorial Bowl"),
    ("Boston College","Syracuse",""),
    # Bowling Green
    ("Bowling Green","Kent State","Battle for the Anniversary Award"),
    ("Bowling Green","Toledo","Battle of I-75"),
    # BYU
    ("BYU","Utah","Holy War"),
    ("BYU","Utah State","Battle for the Old Wagon Wheel"),
    # California
    ("California","Stanford","The Big Game"),
    # Central Michigan
    ("Central Michigan","Eastern Michigan","Battle for the Michigan MAC"),
    ("Central Michigan","Western Michigan","Battle for the Michigan MAC"),
    # Charlotte
    ("Charlotte","Old Dominion",""),
    # Cincinnati
    ("Cincinnati","Louisville","Battle for the Keg of Nails"),
    ("Cincinnati","Miami (OH)","Battle for the Bell"),
    ("Cincinnati","Pittsburgh","River City Rivalry"),
    # Clemson
    ("Clemson","Florida State",""),
    ("Clemson","Georgia",""),
    ("Clemson","Georgia Tech",""),
    ("Clemson","NC State","Textile Bowl"),
    ("Clemson","South Carolina","Palmetto Bowl"),
    # Coastal Carolina
    ("Coastal Carolina","Liberty",""),
    # Colorado
    ("Colorado","Colorado State","Rocky Mountain Showdown"),
    ("Colorado","Nebraska",""),
    ("Colorado","Utah","Rumble in the Rockies"),
    # Colorado State
    ("Colorado State","Wyoming","Battle for the Bronze Boot"),
    # Duke
    ("Duke","North Carolina","Battle for the Victory Bell"),
    ("Duke","Wake Forest",""),
    # East Carolina
    ("East Carolina","North Carolina",""),
    # Eastern Michigan
    ("Eastern Michigan","Western Michigan","Battle for the Michigan MAC"),
    # Florida
    ("Florida","Florida State","Sunshine Showdown"),
    ("Florida","Georgia","Florida-Georgia Game"),
    ("Florida","Miami (FL)","Battle for the Florida Cup"),
    ("Florida","Tennessee","Third Saturday in September"),
    # Florida Atlantic
    ("Florida Atlantic","Florida International","Shula Bowl"),
    # Florida State
    ("Florida State","Miami (FL)","Battle for the Florida Cup"),
    # Fresno State
    ("Fresno State","Hawaii","Battle for the Golden Screwdriver"),
    ("Fresno State","San Jose State","Battle for the Valley Cup"),
    # Georgia
    ("Georgia","Georgia Tech","Clean, Old-Fashioned Hate"),
    ("Georgia","South Carolina",""),
    ("Georgia","Tennessee",""),
    # Georgia Southern
    ("Georgia Southern","Georgia State","Modern Day Hate"),
    # Georgia Tech
    ("Georgia Tech","Vanderbilt",""),
    ("Georgia Tech","Virginia Tech","TechMo Bowl"),
    # Hawaii
    ("Hawaii","UNLV","Island Showdown"),
    ("Hawaii","Wyoming","Battle for the Paniolo"),
    ("Hawaii","San Jose State","Battle for the Dick Tomey Legacy Trophy"),
    # Houston
    ("Houston","Rice","Bayou Bucket Classic"),
    # Illinois
    ("Illinois","Missouri","Arch Rivalry"),
    ("Illinois","Northwestern","Battle for the Land of Lincoln"),
    ("Illinois","Ohio State","Battle for the Illibuck"),
    ("Illinois","Purdue","Battle for the Purdue Cannon"),
    # Indiana
    ("Indiana","Michigan State","Battle for the Old Brass Spittoon"),
    ("Indiana","Purdue","Battle for the Old Oaken Bucket"),
    # Iowa
    ("Iowa","Iowa State","Battle for the Cy-Hawk"),
    ("Iowa","Minnesota","Battle for Floyd of Rosedale"),
    ("Iowa","Nebraska","Battle of the Heroes"),
    ("Iowa","Wisconsin","Battle for the Heartland"),
    # Iowa State
    ("Iowa State","Kansas State","Farmageddon"),
    ("Iowa State","Missouri","Battle for the Telephone"),
    # Jacksonville State
    ("Jacksonville State","Troy","Battle for the Ol' School Bell"),
    # James Madison
    ("James Madison","Old Dominion","Royal Rivalry"),
    # Kansas
    ("Kansas","Kansas State","Sunflower Showdown"),
    ("Kansas","Missouri","The Border War"),
    # Kansas State
    ("Kansas State","Kansas","Sunflower Showdown"),
    # Kentucky
    ("Kentucky","Louisville","Governor's Cup"),
    ("Kentucky","Tennessee",""),
    # Liberty
    ("Liberty","Old Dominion",""),
    # Louisiana
    ("Louisiana","Louisiana-Monroe","Battle on the Bayou"),
    # Louisiana Tech
    ("Louisiana Tech","Louisiana-Monroe",""),
    # Louisville
    ("Louisville","West Virginia",""),
    # LSU
    ("LSU","Ole Miss","Magnolia Bowl"),
    ("LSU","Tulane","Battle for the Rag"),
    # Marshall
    ("Marshall","Ohio","Battle for the Bell"),
    ("Marshall","West Virginia","Friends of the Coal Bowl"),
    # Maryland
    ("Maryland","Navy","Crab Bowl Classic"),
    ("Maryland","Penn State",""),
    ("Maryland","Rutgers",""),
    # Memphis
    ("Memphis","Ole Miss","Mid-South Rivalry"),
    ("Memphis","Southern Mississippi","Black and Blue Bowl"),
    ("Memphis","UAB","Battle of the Bones"),
    # Miami (FL)
    ("Miami (FL)","Virginia Tech",""),
    # Miami (OH)
    ("Miami (OH)","Ohio","Battle of the Bricks"),
    # Michigan
    ("Michigan","Michigan State","Battle for the Paul Bunyan Trophy"),
    ("Michigan","Minnesota","Battle for the Little Brown Jug"),
    ("Michigan","Ohio State","The Game"),
    # Michigan State
    ("Michigan State","Notre Dame","Battle for the Megaphone"),
    ("Michigan State","Penn State","Battle for the Land Grant"),
    # Middle Tennessee
    ("Middle Tennessee","Troy","Battle for the Palladium"),
    ("Middle Tennessee","Western Kentucky","100 Miles of Hate"),
    # Minnesota
    ("Minnesota","Nebraska",""),
    ("Minnesota","Penn State","Battle for the Governor's Victory Bell"),
    ("Minnesota","Wisconsin","Battle for Paul Bunyan's Axe"),
    # Mississippi State
    ("Mississippi State","Ole Miss","Egg Bowl"),
    # Missouri
    ("Missouri","South Carolina","Battle for the Mayor's Cup"),
    # Navy
    ("Navy","Notre Dame","Battle for the Rip Miller Trophy"),
    # Nebraska
    ("Nebraska","Oklahoma",""),
    ("Nebraska","Wisconsin","Battle for Freedom"),
    # Nevada
    ("Nevada","UNLV","Battle for Nevada"),
    # New Mexico
    ("New Mexico","New Mexico State","Rio Grande Rivalry"),
    # New Mexico State
    ("New Mexico State","UTEP","Battle of I-10"),
    # North Carolina
    ("North Carolina","South Carolina","Battle of the Carolinas"),
    ("North Carolina","Virginia","South's Oldest Rivalry"),
    ("North Carolina","Wake Forest",""),
    # NC State
    ("NC State","Wake Forest",""),
    # Northern Illinois
    ("Northern Illinois","Ball State","Battle for the Bronze Stack"),
    # Notre Dame
    ("Notre Dame","Northwestern","Battle for the Shillelagh"),
    ("Notre Dame","Purdue","Battle for the Shillelagh Trophy"),
    ("Notre Dame","Stanford","Battle of the Legends"),
    ("Notre Dame","USC","Battle for the Jeweled Shillelagh"),
    # Ohio State
    ("Ohio State","Penn State",""),
    # Oklahoma
    ("Oklahoma","Oklahoma State","Bedlam"),
    ("Oklahoma","Texas","Red River Rivalry"),
    # Oklahoma State
    ("Oklahoma State","Texas Tech",""),
    ("Oklahoma State","Tulsa","Turnpike Classic"),
    # Oregon
    ("Oregon","Oregon State",""),
    # Penn State
    ("Penn State","Pittsburgh","Keystone Classic"),
    ("Penn State","Syracuse",""),
    # Pittsburgh
    ("Pittsburgh","West Virginia","Backyard Brawl"),
    # Rice
    ("Rice","SMU","Battle for the Mayor's Cup"),
    # SMU
    ("SMU","TCU","Battle for the Iron Skillet"),
    ("SMU","North Texas","Safeway Bowl"),
    # South Alabama
    ("South Alabama","Troy","Battle for the Belt"),
    # Syracuse
    ("Syracuse","West Virginia","Battle for Ben Schwartzwalder Trophy"),
    # TCU
    ("TCU","Texas Tech","Battle for the Saddle"),
    ("TCU","Texas",""),
    # Texas
    ("Texas","Texas A&M","Lone Star Showdown"),
    ("Texas","Texas Tech","Battle for the Chancellor's Spurs"),
    # Texas State
    ("Texas State","UTSA","I-35 Rivalry"),
    # Tulane
    ("Tulane","Southern Mississippi","Battle for the Bell"),
    # UAB
    ("UAB","Troy",""),
    # UCF
    ("UCF","South Florida","War on I-4"),
    ("UCF","Connecticut","Civil ConFLiCT"),
    # UCLA
    ("UCLA","USC","Battle for the Victory Bell"),
    # Utah
    ("Utah","Utah State","Battle of the Brothers"),
    # Utah State
    ("Utah State","Wyoming","Bridger's Battle"),
    # Virginia
    ("Virginia","Virginia Tech","Battle for the Commonwealth Cup"),
    # Washington
    ("Washington","Washington State","Apple Cup"),
    # West Virginia
    ("West Virginia","Virginia Tech","Battle for the Black Diamond"),
    # Wisconsin
    ("Wisconsin","Iowa","Battle for the Heartland"),
    # Wyoming
    ("Wyoming","Colorado State","Battle for the Bronze Boot"),
]

id_counter = 1
for team1, team2, name in rivalries:
    cursor.execute('INSERT INTO rivalries (id, team1, team2, rivalry_name) VALUES (%s, %s, %s, %s)', (id_counter, team1, team2, name))
    id_counter += 1
    cursor.execute('INSERT INTO rivalries (id, team1, team2, rivalry_name) VALUES (%s, %s, %s, %s)', (id_counter, team2, team1, name))
    id_counter += 1

conn.commit()
print(f"Saved {len(rivalries)*2} rivalry entries ({len(rivalries)} unique rivalries)")

cursor.execute("SELECT * FROM rivalries WHERE team1='Penn State' ORDER BY rivalry_name")
for r in cursor.fetchall():
    print(f"  Penn State vs {r[2]}: {r[3]}")


conn.close()
