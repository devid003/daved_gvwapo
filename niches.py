# =====================================================================
#  NICHE DATA — all times are US EASTERN TIME (ET, New York), 24-hour.
#  Each niche has its own best upload windows for EVERY day of the week.
#  Window format: (start_hour, start_min, end_hour, end_min)
# =====================================================================

def week(mon, tue, wed, thu, fri, sat, sun):
    return {0: mon, 1: tue, 2: wed, 3: thu, 4: fri, 5: sat, 6: sun}

YOUTUBE = {
    "commentary": {
        "name": "Commentary", "emoji": "🎙️",
        "days": week(
            [(14, 0, 16, 0), (19, 0, 21, 0)], [(14, 0, 16, 0), (19, 0, 21, 0)],
            [(14, 0, 16, 0), (19, 0, 21, 0)], [(12, 0, 15, 0), (19, 0, 21, 0)],
            [(12, 0, 15, 0), (18, 0, 20, 0)], [(10, 0, 13, 0), (17, 0, 20, 0)],
            [(10, 0, 13, 0), (17, 0, 20, 0)]),
        "hashtags": ["#commentary", "#reaction", "#drama", "#storytime", "#internetdrama",
                     "#youtuberdrama", "#exposed", "#opinion", "#trending", "#viral"],
    },
    "ranking": {
        "name": "Ranking", "emoji": "🏆",
        "days": week(
            [(15, 0, 17, 0), (19, 0, 21, 0)], [(15, 0, 17, 0), (19, 0, 21, 0)],
            [(15, 0, 17, 0), (20, 0, 22, 0)], [(14, 0, 17, 0), (20, 0, 22, 0)],
            [(13, 0, 16, 0), (19, 0, 21, 0)], [(9, 0, 12, 0), (15, 0, 18, 0)],
            [(9, 0, 12, 0), (14, 0, 17, 0)]),
        "hashtags": ["#top10", "#ranking", "#tierlist", "#top5", "#ranked", "#bestof",
                     "#countdown", "#facts", "#comparison", "#versus"],
    },
    "distortion": {
        "name": "Distortion", "emoji": "🔊",
        "days": week(
            [(15, 30, 18, 0), (20, 0, 22, 0)], [(15, 30, 18, 0), (20, 0, 22, 0)],
            [(15, 30, 18, 0), (20, 0, 22, 0)], [(15, 30, 18, 0), (20, 0, 22, 0)],
            [(15, 0, 18, 0), (21, 0, 23, 0)], [(11, 0, 14, 0), (19, 0, 22, 0)],
            [(11, 0, 14, 0), (18, 0, 21, 0)]),
        "hashtags": ["#shorts", "#distortion", "#meme", "#memes", "#funny", "#bassboosted",
                     "#distorted", "#fyp", "#viral", "#funnyshorts"],
    },
    "ranking_commentary": {
        "name": "Ranking + Commentary", "emoji": "🗣️",
        "days": week(
            [(13, 0, 16, 0), (18, 0, 21, 0)], [(13, 0, 16, 0), (18, 0, 21, 0)],
            [(13, 0, 16, 0), (18, 0, 21, 0)], [(13, 0, 16, 0), (19, 0, 21, 0)],
            [(12, 0, 15, 0), (18, 0, 20, 0)], [(10, 0, 13, 0), (16, 0, 19, 0)],
            [(10, 0, 13, 0), (16, 0, 19, 0)]),
        "hashtags": ["#ranking", "#commentary", "#tierlist", "#top10", "#reaction",
                     "#opinion", "#ranked", "#hottakes", "#debate", "#countdown"],
    },
}

FACEBOOK = {
    "ai_drama_pinoy": {
        "name": "AI Drama Pinoy", "emoji": "🇵🇭",
        "days": week(
            [(12, 0, 14, 0), (19, 0, 22, 0)], [(12, 0, 14, 0), (19, 0, 22, 0)],
            [(12, 0, 14, 0), (19, 0, 22, 0)], [(12, 0, 14, 0), (19, 0, 22, 0)],
            [(12, 0, 14, 0), (20, 0, 23, 0)], [(11, 0, 14, 0), (19, 0, 23, 0)],
            [(11, 0, 14, 0), (18, 0, 22, 0)]),
        "hashtags": ["#AIDrama", "#PinoyDrama", "#FilipinoStory", "#FilAm", "#KwentongBuhay",
                     "#DramaSerye", "#AIStory", "#Teleserye", "#PinoyAmerican", "#Reels"],
    },
    "ai_drama_english": {
        "name": "AI Drama English", "emoji": "🎭",
        "days": week(
            [(12, 0, 14, 0), (19, 0, 22, 0)], [(12, 0, 14, 0), (19, 0, 22, 0)],
            [(12, 0, 14, 0), (19, 0, 22, 0)], [(12, 0, 14, 0), (19, 0, 22, 0)],
            [(12, 0, 14, 0), (20, 0, 23, 0)], [(10, 0, 13, 0), (19, 0, 22, 0)],
            [(10, 0, 13, 0), (18, 0, 21, 0)]),
        "hashtags": ["#AIDrama", "#DramaStory", "#ShortDrama", "#Storytime", "#FamilyDrama",
                     "#Karma", "#PlotTwist", "#MiniSeries", "#MustWatch", "#Reels"],
    },
    "home_tips": {
        "name": "Home Tips", "emoji": "🏠",
        "days": week(
            [(9, 0, 11, 0), (13, 0, 15, 0)], [(9, 0, 11, 0), (13, 0, 15, 0)],
            [(9, 0, 11, 0), (13, 0, 15, 0)], [(9, 0, 11, 0), (14, 0, 16, 0)],
            [(9, 0, 11, 0), (12, 0, 14, 0)], [(8, 0, 11, 0), (14, 0, 16, 0)],
            [(8, 0, 11, 0), (15, 0, 17, 0)]),
        "hashtags": ["#HomeTips", "#LifeHacks", "#CleaningHacks", "#HomeHacks", "#CleaningTips",
                     "#DIYHome", "#HomeOrganization", "#HouseholdTips", "#Organization", "#Reels"],
    },
    "cooking": {
        "name": "Cooking", "emoji": "🍳",
        "days": week(
            [(10, 0, 12, 0), (16, 0, 18, 0)], [(10, 0, 12, 0), (16, 0, 18, 0)],
            [(10, 0, 12, 0), (16, 0, 18, 0)], [(10, 0, 12, 0), (16, 0, 18, 0)],
            [(10, 0, 12, 0), (15, 0, 17, 0)], [(9, 0, 12, 0), (15, 0, 18, 0)],
            [(9, 0, 12, 0), (14, 0, 17, 0)]),
        "hashtags": ["#Cooking", "#EasyRecipes", "#DinnerIdeas", "#Recipe", "#HomeCooking",
                     "#FoodLover", "#QuickMeals", "#Foodie", "#ComfortFood", "#Reels"],
    },
    "ghibli": {
        "name": "Ghibli-Style / Cozy Anime", "emoji": "🌿",
        "days": week(
            [(11, 0, 13, 0), (19, 0, 21, 0)], [(11, 0, 13, 0), (19, 0, 21, 0)],
            [(11, 0, 13, 0), (19, 0, 21, 0)], [(11, 0, 13, 0), (19, 0, 21, 0)],
            [(11, 0, 13, 0), (20, 0, 22, 0)], [(10, 0, 12, 0), (18, 0, 21, 0)],
            [(10, 0, 12, 0), (18, 0, 21, 0)]),
        "hashtags": ["#Ghibli", "#GhibliArt", "#AnimeArt", "#CozyVibes", "#AIArt",
                     "#Anime", "#AestheticVideo", "#Relaxing", "#Nostalgia", "#Reels"],
    },
}
